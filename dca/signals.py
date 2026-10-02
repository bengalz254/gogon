"""Entry filter: decides when a new deal may start on a side."""
from __future__ import annotations

from dca.config import EntryConfig
from dca.indicators import ema, rsi
from dca.ladder import LONG


def candles_needed(cfg: EntryConfig) -> int:
    """History the entry filter needs; ~3x the EMA period lets the EMA converge."""
    need = max(500, cfg.rsi_period * 10)
    if cfg.trend_filter:
        need = max(need, cfg.ema_period * 3)
    return need


def entry_signal(side: str, closes: list[float], cfg: EntryConfig) -> tuple[bool, str]:
    """closes: closed candles only, oldest first."""
    reasons = []
    if cfg.trend_filter:
        e = ema(closes, cfg.ema_period)
        if e is None:
            return False, "not enough candles for EMA"
        above = closes[-1] > e
        if (side == LONG) != above:
            return False, f"trend filter: close {closes[-1]:.4f} vs EMA{cfg.ema_period} {e:.4f}"
        reasons.append(f"EMA{cfg.ema_period} ok")

    if cfg.mode == "always":
        return True, ", ".join(reasons + ["mode=always"])

    r = rsi(closes, cfg.rsi_period)
    if r is None:
        return False, "not enough candles for RSI"
    if side == LONG and r <= cfg.long_rsi_below:
        return True, ", ".join(reasons + [f"RSI {r:.1f} <= {cfg.long_rsi_below}"])
    if side != LONG and r >= cfg.short_rsi_above:
        return True, ", ".join(reasons + [f"RSI {r:.1f} >= {cfg.short_rsi_above}"])
    return False, f"RSI {r:.1f}"
