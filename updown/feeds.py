"""Spot price feeds. The bot trades on these, so staleness is tracked explicitly."""
from __future__ import annotations

import bisect
import logging
import threading
import time
from collections import deque

import requests

from updown.config import BINANCE_SYMBOLS, HYPERLIQUID_COINS, FeedConfig
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


class _PollingFeed:
    """Polls one venue for all of its assets in a single request, in a background thread."""

    name = "feed"

    def __init__(self, assets: list[str], cfg: FeedConfig, history: PriceHistory, on_tick=None):
        self.assets = assets
        self.cfg = cfg
        self.history = history
        self.on_tick = on_tick
        self._session = requests.Session()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"{self.name}-feed", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _fetch(self) -> dict[str, float]:
        """Return {asset: price} for this venue's assets."""
        raise NotImplementedError

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            started = time.time()
            try:
                prices = self._fetch()
                now = time.time()
                for asset, price in prices.items():
                    self.history.add(asset, now, price)
                    if self.on_tick:
                        self.on_tick(asset, now, price)
                failures = 0
            except Exception as exc:  # network blips must not kill the feed
                failures += 1
                if failures in (1, 10) or failures % 60 == 0:
                    logger.warning("%s price poll failed (%d in a row): %s", self.name, failures, exc)
            self._stop.wait(max(0.0, self.cfg.poll_seconds - (time.time() - started)))

    def seed_sigma(self, asset: str, minutes: int = 120) -> float | None:
        """Initial volatility from recent 1-minute candles, so we don't start blind."""
        try:
            closes = self._closes_1m(asset, minutes)
        except Exception as exc:
            logger.warning("Could not seed %s volatility from %s candles: %s", asset, self.name, exc)
            return None
        return sigma_from_closes(closes, 60.0)

    def _closes_1m(self, asset: str, minutes: int) -> list[float]:
        raise NotImplementedError


class BinanceFeed(_PollingFeed):
    """Binance spot public ticker (no key needed)."""

    name = "Binance"

    def _fetch(self) -> dict[str, float]:
        symbols = {BINANCE_SYMBOLS[a]: a for a in self.assets}
        resp = self._session.get(
            f"{self.cfg.binance_host}/api/v3/ticker/price",
            params={"symbols": "[" + ",".join(f'"{s}"' for s in symbols) + "]"},
            timeout=3,
        )
        resp.raise_for_status()
        return {symbols[r["symbol"]]: float(r["price"]) for r in resp.json() if r["symbol"] in symbols}

    def _closes_1m(self, asset: str, minutes: int) -> list[float]:
        resp = self._session.get(
            f"{self.cfg.binance_host}/api/v3/klines",
            params={"symbol": BINANCE_SYMBOLS[asset], "interval": "1m", "limit": minutes},
            timeout=5,
        )
        resp.raise_for_status()
        return [float(k[4]) for k in resp.json()]


class HyperliquidFeed(_PollingFeed):
    """Hyperliquid mid prices, for coins Binance spot doesn't list (HYPE)."""

    name = "Hyperliquid"

    def _post(self, body: dict, timeout: float):
        resp = self._session.post(f"{self.cfg.hyperliquid_host}/info", json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _fetch(self) -> dict[str, float]:
        mids = self._post({"type": "allMids"}, 3)
        return {a: float(mids[HYPERLIQUID_COINS[a]]) for a in self.assets if HYPERLIQUID_COINS[a] in mids}

    def _closes_1m(self, asset: str, minutes: int) -> list[float]:
        end = int(time.time() * 1000)
        rows = self._post(
            {"type": "candleSnapshot", "req": {"coin": HYPERLIQUID_COINS[asset], "interval": "1m", "startTime": end - minutes * 60_000, "endTime": end}},
            5,
        )
        return [float(r["c"]) for r in rows]


def build_feeds(assets: list[str], cfg: FeedConfig, history: PriceHistory, on_tick=None) -> dict[str, _PollingFeed]:
    """One feed per venue; returns {asset: the feed that prices it}."""
    by_asset: dict[str, _PollingFeed] = {}
    binance = [a for a in assets if a in BINANCE_SYMBOLS]
    hyper = [a for a in assets if a in HYPERLIQUID_COINS]
    if binance:
        feed = BinanceFeed(binance, cfg, history, on_tick)
        by_asset.update({a: feed for a in binance})
    if hyper:
        feed = HyperliquidFeed(hyper, cfg, history, on_tick)
        by_asset.update({a: feed for a in hyper})
    return by_asset
