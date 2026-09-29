"""Trend following: Donchian breakout with an ATR stop and a trailing exit.

The classic "turtle" idea, meant for higher timeframes (1h-4h), where a
typical move is many times the trading costs:

  * Long when a candle CLOSES above the highest high of the previous
    `entry_bars` candles and the previous candle had not already broken out
    (one signal per breakout, not one per candle). With `trend_ema` > 0 the
    close must also be above that EMA. Short is the mirror image.
  * Initial stop `stop_atr` x ATR from the close.
  * Exit: with `tp_r` = 0 there is no fixed target. The trailing stop from
    `management` (trail_start_r / trail_atr) closes the trade, so the few
    long trends can pay for the many small losses. The order still needs a
    take-profit level, so a far one is used (3x / one third of the price).
    With `tp_r` > 0 a normal target of tp_r x risk is used instead.

Expect a low win rate and long flat or losing stretches between trends. That
is how trend following works, not a bug; judge it on multi-year backtests.
"""
from __future__ import annotations

from collections import deque

from scalper.config import TrendFollowParams
from scalper.indicators import ATR, EMA
from scalper.models import LONG, SHORT, Candle, Signal
from scalper.strategies.base import Strategy

FAR_TARGET = 3.0  # "no target": take-profit at 3x the price (long) or a third of it (short)


class TrendFollowStrategy(Strategy):
    name = "trend_follow"

    def __init__(
        self,
        symbol: str,
        base_ms: int,
        params: TrendFollowParams,
        allow_long: bool = True,
        allow_short: bool = True,
    ):
        super().__init__(symbol, base_ms, allow_long, allow_short)
        self.p = params
        self._atr = ATR(params.atr_period)
        self.ema = EMA(params.trend_ema) if params.trend_ema > 0 else None
        self._highs: deque[float] = deque(maxlen=params.entry_bars)
        self._lows: deque[float] = deque(maxlen=params.entry_bars)
        self._was_above = False
        self._was_below = False

    @property
    def atr(self) -> float | None:
        return self._atr.value

    @property
    def warmup_bars(self) -> int:
        p = self.p
        return max(p.entry_bars + 1, p.atr_period + 1, p.trend_ema + 1)

    def status(self) -> dict:
        out = super().status()
        c = self.last_candle
        if self.ema is not None and self.ema.value is not None and c is not None:
            out["trend"] = LONG if c.close > self.ema.value else SHORT
        return out

    def _on_candle(self, c: Candle) -> Signal | None:
        p = self.p
        atr = self._atr.update(c.high, c.low, c.close)
        ema = self.ema.update(c.close) if self.ema is not None else None

        # Channel of the PREVIOUS entry_bars candles; this candle joins it afterwards.
        full = len(self._highs) == p.entry_bars
        upper = max(self._highs) if full else None
        lower = min(self._lows) if full else None
        self._highs.append(c.high)
        self._lows.append(c.low)
        above = upper is not None and c.close > upper
        below = lower is not None and c.close < lower
        fresh_up = above and not self._was_above
        fresh_down = below and not self._was_below
        self._was_above, self._was_below = above, below

        if atr is None or upper is None or lower is None or (self.ema is not None and ema is None):
            return self._skip("warming up")
        if atr <= 0:
            return self._skip("zero ATR")
        atr_pct = atr / c.close * 100.0
        if not (p.min_atr_pct <= atr_pct <= p.max_atr_pct):
            return self._skip(f"ATR {atr_pct:.3f}% outside [{p.min_atr_pct}, {p.max_atr_pct}]")
        if not (fresh_up or fresh_down):
            return self._skip("no fresh breakout (already broken out)" if above or below else "no breakout")

        dist = p.stop_atr * atr
        if fresh_up:
            if ema is not None and c.close <= ema:
                return self._skip(f"breakout against the EMA{p.trend_ema} trend filter")
            stop = c.close - dist
            target = c.close + p.tp_r * dist if p.tp_r > 0 else c.close * FAR_TARGET
            side, level, word = LONG, upper, "high"
        else:
            if ema is not None and c.close >= ema:
                return self._skip(f"breakout against the EMA{p.trend_ema} trend filter")
            stop = c.close + dist
            target = c.close - p.tp_r * dist if p.tp_r > 0 else c.close / FAR_TARGET
            target = max(target, c.close * 0.05)
            side, level, word = SHORT, lower, "low"
        return Signal(
            symbol=self.symbol,
            side=side,
            strategy=self.name,
            entry_ref=c.close,
            stop=stop,
            take_profit=target,
            atr=atr,
            time=c.close_time,
            tp_r=p.tp_r or None,
            reason=f"close {c.close:.6g} broke the {p.entry_bars}-bar {word} {level:.6g}"
                   + (f" with the EMA{p.trend_ema} trend" if ema is not None else ""),
        )
