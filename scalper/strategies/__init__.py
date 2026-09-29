"""Strategy registry.

To add your own strategy: subclass `Strategy` (see base.py), implement
`_on_candle`, `atr` and `warmup_bars`, add a params dataclass to config.py and
register it in `build_strategy` below. The backtester, paper mode and live
mode will all pick it up with no other changes.
"""
from __future__ import annotations

from scalper.config import Settings
from scalper.strategies.base import Strategy
from scalper.strategies.range_reversion import RangeReversionStrategy
from scalper.strategies.trend_follow import TrendFollowStrategy
from scalper.strategies.trend_pullback import TrendPullbackStrategy


def build_strategy(settings: Settings, symbol: str) -> Strategy:
    st = settings.strategy
    base_ms = settings.interval_ms
    if st.name == "trend_pullback":
        return TrendPullbackStrategy(symbol, base_ms, st.trend_pullback, st.allow_long, st.allow_short)
    if st.name == "range_reversion":
        return RangeReversionStrategy(symbol, base_ms, st.range_reversion, st.allow_long, st.allow_short)
    if st.name == "trend_follow":
        return TrendFollowStrategy(symbol, base_ms, st.trend_follow, st.allow_long, st.allow_short)
    raise ValueError(f"Unknown strategy {st.name!r}")


__all__ = ["Strategy", "TrendPullbackStrategy", "RangeReversionStrategy", "TrendFollowStrategy", "build_strategy"]
