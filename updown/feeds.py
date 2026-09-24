"""Spot price feeds. The bot trades on these, so staleness is tracked explicitly."""
from __future__ import annotations

import bisect
import logging
import threading
import time
from collections import deque

import requests

from updown.config import BINANCE_SYMBOLS, FeedConfig
from updown.model import sigma_from_closes

logger = logging.getLogger("polybot.updown.feed")


class PriceHistory:
    """Per-asset (timestamp, price) samples, oldest first."""

    def __init__(self, maxlen: int = 3600):
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        self._maxlen = maxlen
        self._lock = threading.Lock()

    def add(self, asset: str, ts: float, price: float) -> None:
        with self._lock:
            q = self._samples.setdefault(asset, deque(maxlen=self._maxlen))
            if q and ts <= q[-1][0]:
                return
            q.append((ts, price))

    def latest(self, asset: str) -> tuple[float, float] | None:
        with self._lock:
            q = self._samples.get(asset)
            return q[-1] if q else None

    def price_at(self, asset: str, ts: float, tolerance_s: float) -> float | None:
        """Last price at or before `ts`, if one exists within `tolerance_s`."""
        with self._lock:
            q = self._samples.get(asset)
            if not q:
                return None
            samples = list(q)
        i = bisect.bisect_right([s[0] for s in samples], ts) - 1
        if i < 0 or ts - samples[i][0] > tolerance_s:
            return None
        return samples[i][1]


class BinanceFeed:
    """Polls Binance's public ticker for all assets in one request, in a background thread."""

    def __init__(self, assets: list[str], cfg: FeedConfig, history: PriceHistory, on_tick=None):
        self.assets = assets
        self.cfg = cfg
        self.history = history
        self.on_tick = on_tick
        self._symbols = {BINANCE_SYMBOLS[a]: a for a in assets}
        self._session = requests.Session()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="binance-feed", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        symbols = "[" + ",".join(f'"{s}"' for s in self._symbols) + "]"
        url = f"{self.cfg.binance_host}/api/v3/ticker/price"
        failures = 0
        while not self._stop.is_set():
            started = time.time()
            try:
                resp = self._session.get(url, params={"symbols": symbols}, timeout=3)
                resp.raise_for_status()
                now = time.time()
                for row in resp.json():
                    asset = self._symbols.get(row["symbol"])
                    if asset:
                        price = float(row["price"])
                        self.history.add(asset, now, price)
                        if self.on_tick:
                            self.on_tick(asset, now, price)
                failures = 0
            except Exception as exc:  # network blips must not kill the feed
                failures += 1
                if failures in (1, 10) or failures % 60 == 0:
                    logger.warning("Binance price poll failed (%d in a row): %s", failures, exc)
            self._stop.wait(max(0.0, self.cfg.poll_seconds - (time.time() - started)))

    def seed_sigma(self, asset: str, minutes: int = 120) -> float | None:
        """Initial volatility from recent 1-minute candles, so we don't start blind."""
        try:
            resp = self._session.get(
                f"{self.cfg.binance_host}/api/v3/klines",
                params={"symbol": BINANCE_SYMBOLS[asset], "interval": "1m", "limit": minutes},
                timeout=5,
            )
            resp.raise_for_status()
            closes = [float(k[4]) for k in resp.json()]
        except Exception as exc:
            logger.warning("Could not seed %s volatility from klines: %s", asset, exc)
            return None
        return sigma_from_closes(closes, 60.0)
