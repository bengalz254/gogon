"""EMA-cross signal logic shared by the backtester and the live bot.

Rules:
  * Only CLOSED candles are ever used — the candle still forming is dropped.
  * EMA fast crosses ABOVE EMA slow on a closed candle  -> "LONG"
  * EMA fast crosses BELOW EMA slow on a closed candle  -> "SHORT"
  * No cross                                            -> None (bot stays idle)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from hlbot.indicators import ema

LONG = "LONG"
SHORT = "SHORT"

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError:
        raise ValueError(f"Unsupported interval {interval!r}; use one of {sorted(INTERVAL_MS)}") from None


@dataclass(frozen=True)
class Candle:
    t: int  # open time, ms since epoch (UTC)
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: float = 0.0


def closed_candles(candles: Sequence[Candle], interval: str, now_ms: int) -> list[Candle]:
    """Drop any candle that has not fully closed by `now_ms`."""
    span = interval_ms(interval)
    return [c for c in candles if c.t + span <= now_ms]


def cross_at(fast: Sequence[Optional[float]], slow: Sequence[Optional[float]], i: int) -> Optional[str]:
    """Signal produced by the close of candle `i` (compares candle i-1 vs i)."""
    if i < 1:
        return None
    f0, s0, f1, s1 = fast[i - 1], slow[i - 1], fast[i], slow[i]
    if f0 is None or s0 is None or f1 is None or s1 is None:
        return None
    prev_diff = f0 - s0
    diff = f1 - s1
    if prev_diff <= 0 < diff:
        return LONG
    if prev_diff >= 0 > diff:
        return SHORT
    return None


def latest_signal(closes: Sequence[float], fast_period: int, slow_period: int) -> Optional[str]:
    """Signal from the most recent candle in `closes` (which must all be closed)."""
    fast = ema(closes, fast_period)
    slow = ema(closes, slow_period)
    return cross_at(fast, slow, len(closes) - 1)


def trend(closes: Sequence[float], fast_period: int, slow_period: int) -> Optional[str]:
    """Which side of the slow EMA the fast EMA is currently on (informational)."""
    fast = ema(closes, fast_period)[-1] if closes else None
    slow = ema(closes, slow_period)[-1] if closes else None
    if fast is None or slow is None or fast == slow:
        return None
    return LONG if fast > slow else SHORT
