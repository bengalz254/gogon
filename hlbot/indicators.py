"""Technical indicators (pure functions, no I/O)."""
from __future__ import annotations

from typing import Optional, Sequence


def ema(values: Sequence[float], period: int) -> list[Optional[float]]:
    """Exponential moving average, seeded with the SMA of the first `period`
    values (same convention as TradingView's `ta.ema`).

    Returns a list the same length as `values`; entries before the EMA has
    enough data are None.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out
