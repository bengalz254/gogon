"""Trend-pullback scalper (default strategy).

Idea: only trade WITH the higher-timeframe trend, and only after price has
pulled back toward value and then shown it is resuming. Chasing extended
candles and fading strong trends are the two most common ways scalpers bleed;
this setup avoids both.

Long setup (short is the mirror image):
  1. Higher timeframe (default 1h) is in an uptrend: close > slow EMA and
     fast EMA > slow EMA. Uses only COMPLETED higher-timeframe candles.
  2. Base timeframe trend agrees: fast EMA > slow EMA, close > slow EMA.
  3. Trend has some strength: ADX >= adx_min (optional).
  4. Pullback happened in the last `pullback_lookback` bars: a low came
     within `pullback_atr_tolerance` ATR of the fast EMA, and RSI dipped to
     `rsi_pullback_long` or below.
  5. Resumption trigger on this closed bar: bullish candle closing above the
     fast EMA and above the previous bar's high, with RSI back above
     `rsi_trigger_long`.
  6. Volatility sanity: ATR% within [min_atr_pct, max_atr_pct] and the
     trigger bar is not a news spike (range <= max_bar_atr × ATR).

Stop: below the recent swing low minus a small ATR buffer, at least
`min_sl_atr` ATR away. Setups needing a stop wider than `max_sl_atr` ATR are
skipped (bad reward:risk). Take-profit: `tp_r` × risk, re-anchored to the
actual fill price.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from scalper.config import TrendPullbackParams
from scalper.indicators import ADX, ATR, EMA, RSI, Resampler
from scalper.models import INTERVAL_MS, LONG, SHORT, Candle, Signal
from scalper.strategies.base import Strategy


@dataclass
class _Bar:
    candle: Candle
    ema_fast: float | None
    rsi: float | None
    atr: float | None


class TrendPullbackStrategy(Strategy):
    name = "trend_pullback"

    def __init__(
        self,
        symbol: str,
        base_ms: int,
        params: TrendPullbackParams,
        allow_long: bool = True,
        allow_short: bool = True,
    ):
        super().__init__(symbol, base_ms, allow_long, allow_short)
        p = self.p = params
        self.ema_fast = EMA(p.ema_fast)
        self.ema_slow = EMA(p.ema_slow)
        self.rsi = RSI(p.rsi_period)
        self._atr = ATR(p.atr_period)
        self.adx = ADX(p.adx_period)
        self.htf = Resampler(base_ms, INTERVAL_MS[p.htf_interval])
        self.htf_fast = EMA(p.htf_ema_fast)
        self.htf_slow = EMA(p.htf_ema_slow)
        self.htf_close: float | None = None
        self._hist: deque[_Bar] = deque(maxlen=max(p.pullback_lookback, p.swing_lookback, 2))

    @property
    def atr(self) -> float | None:
        return self._atr.value

    @property
    def warmup_bars(self) -> int:
        p = self.p
        htf_ratio = INTERVAL_MS[p.htf_interval] // self.base_ms
        base_need = max(p.ema_slow, 2 * p.adx_period + 1, p.rsi_period + 1, p.atr_period)
        # +2 higher-timeframe bars: one may be dropped as partial at the start.
        return max(base_need, htf_ratio * (p.htf_ema_slow + 2))

    def htf_trend(self) -> str | None:
        if not (self.htf_fast.ready and self.htf_slow.ready and self.htf_close is not None):
            return None
        fast, slow, close = self.htf_fast.value, self.htf_slow.value, self.htf_close
        if close > slow and fast > slow:
            return LONG
        if close < slow and fast < slow:
            return SHORT
        return None

    def _on_candle(self, c: Candle) -> Signal | None:
        p = self.p
        for h in self.htf.update(c):
            self.htf_fast.update(h.close)
            self.htf_slow.update(h.close)
            self.htf_close = h.close

        ef = self.ema_fast.update(c.close)
        es = self.ema_slow.update(c.close)
        rsi = self.rsi.update(c.close)
        atr = self._atr.update(c.high, c.low, c.close)
        adx = self.adx.update(c.high, c.low, c.close)

        prev = self._hist[-1] if self._hist else None
        self._hist.append(_Bar(c, ef, rsi, atr))

        if ef is None or es is None or rsi is None or atr is None or prev is None:
            return self._skip("warming up")
        trend = self.htf_trend()
        if trend is None:
            return self._skip("no clear higher-timeframe trend")
        if p.adx_min > 0 and (adx is None or adx < p.adx_min):
            return self._skip(f"ADX {adx if adx is None else round(adx, 1)} < {p.adx_min}")
        if atr <= 0:
            return self._skip("zero ATR")
        atr_pct = atr / c.close * 100.0
        if not (p.min_atr_pct <= atr_pct <= p.max_atr_pct):
            return self._skip(f"ATR {atr_pct:.3f}% outside [{p.min_atr_pct}, {p.max_atr_pct}]")
        if c.range > p.max_bar_atr * atr:
            return self._skip("trigger bar is a volatility spike")

        window = list(self._hist)[-p.pullback_lookback:]
        swing = list(self._hist)[-p.swing_lookback:]

        if trend == LONG and ef > es and c.close > es:
            touched = any(
                b.ema_fast is not None and b.atr is not None
                and b.candle.low <= b.ema_fast + p.pullback_atr_tolerance * b.atr
                for b in window
            )
            dipped = any(b.rsi is not None and b.rsi <= p.rsi_pullback_long for b in window)
            trigger = (
                c.close > c.open
                and c.close > ef
                and c.close > prev.candle.high
                and rsi >= p.rsi_trigger_long
            )
            if not (touched and dipped and trigger):
                return self._skip("long setup incomplete")
            stop = min(b.candle.low for b in swing) - p.sl_buffer_atr * atr
            dist = c.close - stop
            if dist < p.min_sl_atr * atr:
                dist = p.min_sl_atr * atr
                stop = c.close - dist
            if dist > p.max_sl_atr * atr:
                return self._skip(f"stop {dist / atr:.2f} ATR away > max_sl_atr")
            return Signal(
                symbol=self.symbol,
                side=LONG,
                strategy=self.name,
                entry_ref=c.close,
                stop=stop,
                take_profit=c.close + p.tp_r * dist,
                atr=atr,
                time=c.close_time,
                tp_r=p.tp_r,
                reason=(
                    f"HTF up, pullback to EMA{p.ema_fast} with RSI dip, "
                    f"resumed above prev high (RSI {rsi:.1f}, ADX {adx:.1f})"
                    if adx is not None else "HTF up, pullback resumed"
                ),
            )

        if trend == SHORT and ef < es and c.close < es:
            touched = any(
                b.ema_fast is not None and b.atr is not None
                and b.candle.high >= b.ema_fast - p.pullback_atr_tolerance * b.atr
                for b in window
            )
            popped = any(b.rsi is not None and b.rsi >= p.rsi_pullback_short for b in window)
            trigger = (
                c.close < c.open
                and c.close < ef
                and c.close < prev.candle.low
                and rsi <= p.rsi_trigger_short
            )
            if not (touched and popped and trigger):
                return self._skip("short setup incomplete")
            stop = max(b.candle.high for b in swing) + p.sl_buffer_atr * atr
            dist = stop - c.close
            if dist < p.min_sl_atr * atr:
                dist = p.min_sl_atr * atr
                stop = c.close + dist
            if dist > p.max_sl_atr * atr:
                return self._skip(f"stop {dist / atr:.2f} ATR away > max_sl_atr")
            return Signal(
                symbol=self.symbol,
                side=SHORT,
                strategy=self.name,
                entry_ref=c.close,
                stop=stop,
                take_profit=c.close - p.tp_r * dist,
                atr=atr,
                time=c.close_time,
                tp_r=p.tp_r,
                reason=(
                    f"HTF down, pullback to EMA{p.ema_fast} with RSI pop, "
                    f"resumed below prev low (RSI {rsi:.1f}, ADX {adx:.1f})"
                    if adx is not None else "HTF down, pullback resumed"
                ),
            )
        return self._skip("base timeframe does not agree with higher-timeframe trend")
