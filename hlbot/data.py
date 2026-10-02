"""Market data: Hyperliquid public REST API + CSV import/export."""
from __future__ import annotations

import csv
import os
import time
from typing import Optional

import requests

from hlbot.strategy import Candle, interval_ms

# Hyperliquid only serves the most recent 5000 candles per coin/interval.
MAX_CANDLES_PER_REQUEST = 5000


class HyperliquidInfo:
    """Minimal read-only client for POST /info (no wallet needed)."""

    def __init__(self, base_url: str, timeout: float = 15.0, session: Optional[requests.Session] = None):
        self.url = base_url.rstrip("/") + "/info"
        self.timeout = timeout
        self.session = session or requests.Session()

    def _post(self, payload: dict):
        resp = self.session.post(self.url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def all_mids(self) -> dict[str, float]:
        return {k: float(v) for k, v in self._post({"type": "allMids"}).items()}

    def mid_price(self, coin: str) -> float:
        mids = self.all_mids()
        if coin not in mids:
            raise KeyError(f"Coin {coin!r} not found on Hyperliquid")
        return mids[coin]

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        """Candles with open time in [start_ms, end_ms], oldest first. Paginates
        and de-duplicates. Includes the still-forming candle if in range —
        callers must filter with `strategy.closed_candles`."""
        span = interval_ms(interval)
        out: dict[int, Candle] = {}
        cursor = start_ms
        while cursor <= end_ms:
            raw = self._post(
                {
                    "type": "candleSnapshot",
                    "req": {"coin": coin, "interval": interval, "startTime": cursor, "endTime": end_ms},
                }
            )
            if not raw:
                break
            for r in raw:
                c = Candle(
                    t=int(r["t"]), o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"]),
                    v=float(r.get("v", 0.0)),
                )
                out[c.t] = c
            last_t = max(int(r["t"]) for r in raw)
            if last_t + span <= cursor or len(raw) < 2:
                break
            cursor = last_t + span
        return [out[t] for t in sorted(out)]

    def recent_candles(self, coin: str, interval: str, count: int, now_ms: Optional[int] = None) -> list[Candle]:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        start = now_ms - count * interval_ms(interval)
        return self.candles(coin, interval, start, now_ms)


def save_csv(candles: list[Candle], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["open_time_ms", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.t, c.o, c.h, c.l, c.c, c.v])


def load_csv(path: str) -> list[Candle]:
    """Load candles from CSV. Accepts this project's own format (header row
    `open_time_ms,open,high,low,close,volume`) or a headerless Binance kline
    export from data.binance.vision (open_time, o, h, l, c, v, ...).
    Timestamps in seconds or microseconds are normalised to milliseconds."""
    candles: dict[int, Candle] = {}
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip():
                continue
            try:
                ts = int(float(row[0]))
            except ValueError:
                continue  # header row
            if ts < 10**11:  # seconds
                ts *= 1000
            elif ts > 10**14:  # microseconds
                ts //= 1000
            vol = float(row[5]) if len(row) > 5 and row[5] else 0.0
            candles[ts] = Candle(t=ts, o=float(row[1]), h=float(row[2]), l=float(row[3]), c=float(row[4]), v=vol)
    return [candles[t] for t in sorted(candles)]
