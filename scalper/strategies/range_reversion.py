"""Range mean-reversion scalper (optional, off by default).

Idea: when the market is ranging (low ADX), a close outside the Bollinger
Band with an RSI extreme tends to snap back toward the mean. The entry waits
for the snap-back to START (the next candle closes back inside the band in
the reversal direction) instead of catching a falling knife.

Long setup (short is the mirror image):
  1. Regime: ADX <= adx_max (no strong trend).
  2. Previous bar closed below the lower band with RSI <= rsi_oversold.
  3. This bar is bullish and closes back above the lower band.
  4. Reward:risk to the middle band >= min_rr.

Stop: below the recent swing low minus an ATR buffer, and at least
`min_sl_atr` ATR away (tight stops get wicked out). Target: the band's
middle (the mean) as measured at signal time — a fixed level, not an R
multiple.

This strategy loses in trends; the ADX filter reduces but cannot remove
that. Backtest it on your symbols before enabling it.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from scalper.config import RangeReversionParams
from scalper.indicators import ADX, ATR, RSI, Bollinger
from scalper.models import LONG, SHORT, Candle, Signal
from scalper.strategies.base import Strategy


@dataclass
class _Bar:
    candle: Candle
    lower: float | None
    upper: float | None
    rsi: float | None


class RangeReversionStrategy(Strategy):
    name = "range_reversion"

    def __init__(
        self,
        symbol: str,
        base_ms: int,
        params: RangeReversionParams,
        allow_long: bool = True,
        allow_short: bool = True,
    ):
        super().__init__(symbol, base_ms, allow_long, allow_short)
        p = self.p = params
        self.bb = Bollinger(p.bb_period, p.bb_std)
        self.rsi = RSI(p.rsi_period)
        self.adx = ADX(p.adx_period)
        self._atr = ATR(p.atr_period)
        self._hist: deque[_Bar] = deque(maxlen=max(p.swing_lookback, 2))

    @property
    def atr(self) -> float | None:
        return self._atr.value

    @property
    def warmup_bars(self) -> int:
        p = self.p
        return max(p.bb_period, 2 * p.adx_period + 1, p.rsi_period + 1, p.atr_period) + 1

    def _on_candle(self, c: Candle) -> Signal | None:
        p = self.p
        mid = self.bb.update(c.close)
        rsi = self.rsi.update(c.close)
        atr = self._atr.update(c.high, c.low, c.close)
        adx = self.adx.update(c.high, c.low, c.close)

        prev = self._hist[-1] if self._hist else None
        self._hist.append(_Bar(c, self.bb.lower, self.bb.upper, rsi))

        if mid is None or rsi is None or atr is None or adx is None or prev is None:
            return self._skip("warming up")
        if prev.lower is None or prev.upper is None or prev.rsi is None:
            return self._skip("warming up")
        if adx > p.adx_max:
            return self._skip(f"trending (ADX {adx:.1f} > {p.adx_max})")
        if atr <= 0:
            return self._skip("zero ATR")
        atr_pct = atr / c.close * 100.0
        if not (p.min_atr_pct <= atr_pct <= p.max_atr_pct):
            return self._skip(f"ATR {atr_pct:.3f}% outside [{p.min_atr_pct}, {p.max_atr_pct}]")
        if c.range > p.max_bar_atr * atr:
            return self._skip("bar is a volatility spike")

        swing = list(self._hist)[-p.swing_lookback:]

        if (
            prev.candle.close < prev.lower
            and prev.rsi <= p.rsi_oversold
            and c.close > c.open
            and c.close > self.bb.lower
        ):
            stop = min(b.candle.low for b in swing) - p.sl_buffer_atr * atr
            stop = min(stop, c.close - p.min_sl_atr * atr)
            risk = c.close - stop
            reward = mid - c.close
            if risk <= 0 or reward <= 0 or reward / risk < p.min_rr:
                return self._skip("long reversion reward:risk too small")
            return Signal(
                symbol=self.symbol,
                side=LONG,
                strategy=self.name,
                entry_ref=c.close,
                stop=stop,
                take_profit=mid,
                atr=atr,
                time=c.close_time,
                tp_r=None,
                reason=f"close back inside lower band after RSI {prev.rsi:.1f}, ADX {adx:.1f}",
            )

        if (
            prev.candle.close > prev.upper
            and prev.rsi >= p.rsi_overbought
            and c.close < c.open
            and c.close < self.bb.upper
        ):
            stop = max(b.candle.high for b in swing) + p.sl_buffer_atr * atr
            stop = max(stop, c.close + p.min_sl_atr * atr)
            risk = stop - c.close
            reward = c.close - mid
            if risk <= 0 or reward <= 0 or reward / risk < p.min_rr:
                return self._skip("short reversion reward:risk too small")
            return Signal(
                symbol=self.symbol,
                side=SHORT,
                strategy=self.name,
                entry_ref=c.close,
                stop=stop,
                take_profit=mid,
                atr=atr,
                time=c.close_time,
                tp_r=None,
                reason=f"close back inside upper band after RSI {prev.rsi:.1f}, ADX {adx:.1f}",
            )
        return self._skip("no band re-entry")
