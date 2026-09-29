"""EMA crossover: long when the fast EMA crosses above the slow one, short
when it crosses below (default EMA 9 / EMA 21).

  * Entry right after the candle that closes with the cross (the backtest
    fills at the next open; there is no earlier moment without look-ahead).
  * Stop `sl_atr` x ATR from the close, but never closer than `min_sl_pct`
    of the price: on small timeframes an ATR stop can be so tight that the
    round-trip fees would be a large part of every loss.
  * Take profit `tp_r` x the stop distance.
  * With management.exit_on_opposite_signal the opposite cross closes the
    trade and opens the new direction (stop-and-reverse).
  * Optional `trend_ema`: only open trades in that EMA's direction. An
    opposite cross against it still closes the open trade, it just does not
    open the other way.

Crossovers are the simplest trend signal there is; on low timeframes they
whipsaw a lot, so judge the settings on a backtest that includes fees.
"""
from __future__ import annotations

from scalper.config import EmaCrossParams
from scalper.indicators import ATR, EMA
from scalper.models import LONG, SHORT, Candle, Signal
from scalper.strategies.base import Strategy


class EmaCrossStrategy(Strategy):
    name = "ema_cross"

    def __init__(
        self,
        symbol: str,
        base_ms: int,
        params: EmaCrossParams,
        allow_long: bool = True,
        allow_short: bool = True,
    ):
        super().__init__(symbol, base_ms, allow_long, allow_short)
        self.p = params
        self.fast = EMA(params.fast)
        self.slow = EMA(params.slow)
        self._atr = ATR(params.atr_period)
        self.trend = EMA(params.trend_ema) if params.trend_ema > 0 else None
        self._prev: tuple[float, float] | None = None  # (fast, slow) at the previous close
        self._cross: str | None = None  # direction of a cross on the latest candle

    @property
    def atr(self) -> float | None:
        return self._atr.value

    def should_exit(self, side: str) -> bool:
        # every opposite cross closes the trade, also one the trend filter
        # (or allow_long / allow_short) keeps from opening the other way
        return self._cross is not None and self._cross != side

    @property
    def warmup_bars(self) -> int:
        p = self.p
        return max(p.slow + 1, p.atr_period + 1, p.trend_ema + 1)

    def status(self) -> dict:
        out = super().status()
        if self.fast.value is not None and self.slow.value is not None:
            out["trend"] = LONG if self.fast.value > self.slow.value else SHORT
        return out

    def _on_candle(self, c: Candle) -> Signal | None:
        p = self.p
        fast = self.fast.update(c.close)
        slow = self.slow.update(c.close)
        atr = self._atr.update(c.high, c.low, c.close)
        trend = self.trend.update(c.close) if self.trend is not None else None
        prev = self._prev
        self._prev = (fast, slow) if fast is not None and slow is not None else None
        self._cross = None

        if prev is None or fast is None or slow is None or atr is None or (self.trend is not None and trend is None):
            return self._skip("warming up")
        crossed_up = prev[0] <= prev[1] and fast > slow
        crossed_down = prev[0] >= prev[1] and fast < slow
        if not (crossed_up or crossed_down):
            return self._skip("no EMA cross")
        side = LONG if crossed_up else SHORT
        self._cross = side
        if atr <= 0:
            return self._skip("zero ATR")
        if trend is not None and ((side == LONG and c.close <= trend) or (side == SHORT and c.close >= trend)):
            return self._skip(f"cross against the EMA{p.trend_ema} trend filter")

        dist = max(p.sl_atr * atr, c.close * p.min_sl_pct / 100.0)
        sign = 1.0 if side == LONG else -1.0
        return Signal(
            symbol=self.symbol,
            side=side,
            strategy=self.name,
            entry_ref=c.close,
            stop=c.close - sign * dist,
            take_profit=c.close + sign * p.tp_r * dist,
            atr=atr,
            time=c.close_time,
            tp_r=p.tp_r,
            reason=f"EMA{p.fast} crossed {'above' if side == LONG else 'below'} EMA{p.slow}",
        )
