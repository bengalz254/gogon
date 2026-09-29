"""Strategy interface.

A strategy is a per-symbol object fed one CLOSED candle at a time. It keeps
its own indicator state and may return a Signal describing a trade idea:
direction, stop-loss level and take-profit level. Everything else — whether
the trade is allowed, how big it is, how it is executed and managed — is
decided outside the strategy, identically in backtest, paper and live mode.
"""
from __future__ import annotations

from scalper.models import LONG, SHORT, Candle, Signal


class Strategy:
    name = "base"

    def __init__(self, symbol: str, base_ms: int, allow_long: bool = True, allow_short: bool = True):
        self.symbol = symbol
        self.base_ms = base_ms
        self.allow_long = allow_long
        self.allow_short = allow_short
        self.bars_seen = 0
        self.last_candle: Candle | None = None
        # Human-readable reason the most recent candle did NOT produce a
        # signal; handy when debugging "why didn't it trade?".
        self.last_skip_reason = ""

    # -- to implement -------------------------------------------------
    @property
    def atr(self) -> float | None:
        raise NotImplementedError

    @property
    def warmup_bars(self) -> int:
        """Closed base candles needed before signals can be produced."""
        raise NotImplementedError

    def _on_candle(self, c: Candle) -> Signal | None:
        raise NotImplementedError

    # -- public -------------------------------------------------------
    def on_candle(self, c: Candle) -> Signal | None:
        if self.last_candle is not None and c.open_time <= self.last_candle.open_time:
            return None  # duplicate / out-of-order candle: ignore
        self.bars_seen += 1
        self.last_skip_reason = ""
        sig = self._on_candle(c)
        self.last_candle = c
        if sig is None:
            return None
        if (sig.side == LONG and not self.allow_long) or (sig.side == SHORT and not self.allow_short):
            self.last_skip_reason = f"{sig.side} disabled in config"
            return None
        return sig

    def _skip(self, reason: str) -> None:
        self.last_skip_reason = reason
        return None

    def should_exit(self, side: str) -> bool:
        """True if an open `side` trade should be closed at this close even
        though no opposite entry signal came (e.g. it was filtered out).
        Only used with management.exit_on_opposite_signal."""
        return False

    def status(self) -> dict:
        """Market readings behind the latest decision, for the dashboard."""
        out: dict = {}
        c = self.last_candle
        if self.atr and c is not None and c.close > 0:
            out["atr_pct"] = round(self.atr / c.close * 100.0, 4)
        return out
