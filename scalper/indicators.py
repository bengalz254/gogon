"""Incremental (streaming) technical indicators.

Every indicator is fed one CLOSED candle at a time and only ever looks
backwards, so the exact same objects drive the backtester, paper trading and
live trading — there is no vectorised code path that could accidentally peek
at future bars.

Smoothing follows J. Welles Wilder's original definitions (RSI, ATR, ADX),
which is what TradingView and TA-Lib use, so values line up with the charts
you look at.
"""
from __future__ import annotations

import math
from collections import deque

from scalper.models import Candle


class EMA:
    """Exponential moving average, seeded with the SMA of the first `period` values."""

    def __init__(self, period: int):
        if period < 1:
            raise ValueError("EMA period must be >= 1")
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: float | None = None
        self._seed: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
                self._seed = []
            return self.value
        self.value += self.alpha * (x - self.value)
        return self.value


class SMA:
    def __init__(self, period: int):
        self.period = period
        self._win: deque[float] = deque(maxlen=period)
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, x: float) -> float | None:
        self._win.append(x)
        if len(self._win) == self.period:
            # Windows are small, so summing directly is cheap and avoids the
            # float drift a running total accumulates over months of candles.
            self.value = sum(self._win) / self.period
        return self.value


class RSI:
    """Wilder's Relative Strength Index."""

    def __init__(self, period: int = 14):
        if period < 1:
            raise ValueError("RSI period must be >= 1")
        self.period = period
        self.value: float | None = None
        self._prev: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def _compute(self) -> float:
        if self._avg_loss == 0:
            return 100.0 if self._avg_gain and self._avg_gain > 0 else 50.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        n = self.period
        if self._avg_gain is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) == n:
                self._avg_gain = sum(self._gains) / n
                self._avg_loss = sum(self._losses) / n
                self._gains, self._losses = [], []
                self.value = self._compute()
            return self.value
        self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
        self._avg_loss = (self._avg_loss * (n - 1) + loss) / n
        self.value = self._compute()
        return self.value


class ATR:
    """Wilder's Average True Range."""

    def __init__(self, period: int = 14):
        if period < 1:
            raise ValueError("ATR period must be >= 1")
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._trs: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        if self.value is None:
            self._trs.append(tr)
            if len(self._trs) == self.period:
                self.value = sum(self._trs) / self.period
                self._trs = []
            return self.value
        self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class ADX:
    """Wilder's Average Directional Index (trend strength, 0-100)."""

    def __init__(self, period: int = 14):
        if period < 1:
            raise ValueError("ADX period must be >= 1")
        self.period = period
        self.value: float | None = None
        self.plus_di: float | None = None
        self.minus_di: float | None = None
        self._prev: tuple[float, float, float] | None = None
        self._tr_s: float | None = None
        self._pdm_s = 0.0
        self._mdm_s = 0.0
        self._init: list[tuple[float, float, float]] = []
        self._dx_init: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev is None:
            self._prev = (high, low, close)
            return None
        ph, pl, pc = self._prev
        self._prev = (high, low, close)

        up = high - ph
        down = pl - low
        pdm = up if (up > down and up > 0) else 0.0
        mdm = down if (down > up and down > 0) else 0.0
        tr = max(high - low, abs(high - pc), abs(low - pc))
        n = self.period

        if self._tr_s is None:
            self._init.append((tr, pdm, mdm))
            if len(self._init) < n:
                return None
            self._tr_s = sum(t[0] for t in self._init)
            self._pdm_s = sum(t[1] for t in self._init)
            self._mdm_s = sum(t[2] for t in self._init)
            self._init = []
        else:
            self._tr_s = self._tr_s - self._tr_s / n + tr
            self._pdm_s = self._pdm_s - self._pdm_s / n + pdm
            self._mdm_s = self._mdm_s - self._mdm_s / n + mdm

        if self._tr_s > 0:
            pdi = 100.0 * self._pdm_s / self._tr_s
            mdi = 100.0 * self._mdm_s / self._tr_s
        else:
            pdi = mdi = 0.0
        self.plus_di, self.minus_di = pdi, mdi
        di_sum = pdi + mdi
        dx = 0.0 if di_sum == 0 else 100.0 * abs(pdi - mdi) / di_sum

        if self.value is None:
            self._dx_init.append(dx)
            if len(self._dx_init) == n:
                self.value = sum(self._dx_init) / n
                self._dx_init = []
            return self.value
        self.value = (self.value * (n - 1) + dx) / n
        return self.value


class Bollinger:
    """Bollinger Bands with population standard deviation (TradingView convention)."""

    def __init__(self, period: int = 20, num_std: float = 2.0):
        self.period = period
        self.num_std = num_std
        self._win: deque[float] = deque(maxlen=period)
        self.mid: float | None = None
        self.upper: float | None = None
        self.lower: float | None = None

    @property
    def ready(self) -> bool:
        return self.mid is not None

    def update(self, x: float) -> float | None:
        self._win.append(x)
        if len(self._win) < self.period:
            return None
        n = self.period
        mean = sum(self._win) / n
        var = sum((v - mean) ** 2 for v in self._win) / n
        std = math.sqrt(var)
        self.mid = mean
        self.upper = mean + self.num_std * std
        self.lower = mean - self.num_std * std
        return self.mid


class Resampler:
    """Builds higher-timeframe candles from a stream of closed base candles.

    Only COMPLETED higher-timeframe candles are emitted, so a trend filter
    built on them never sees the still-forming bar (no look-ahead). If the
    stream starts in the middle of a bucket, that first partial bucket is
    dropped instead of being passed off as a full candle.
    """

    def __init__(self, base_ms: int, target_ms: int):
        if target_ms < base_ms or target_ms % base_ms != 0:
            raise ValueError("higher timeframe must be a whole multiple of the base timeframe")
        self.base_ms = base_ms
        self.target_ms = target_ms
        self._cur: Candle | None = None
        self._cur_partial = False
        self._started = False

    def update(self, c: Candle) -> list[Candle]:
        done: list[Candle] = []
        bucket = c.open_time - (c.open_time % self.target_ms)

        if self._cur is not None and self._cur.open_time != bucket:
            # The previous bucket never received its final base candle (e.g.
            # exchange maintenance gap). It still only contains past data, so
            # emit it now.
            if not self._cur_partial:
                done.append(self._cur)
            self._cur = None

        if self._cur is None:
            self._cur = Candle(
                open_time=bucket,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                close_time=bucket + self.target_ms - 1,
            )
            self._cur_partial = (not self._started) and c.open_time != bucket
            self._started = True
        else:
            cur = self._cur
            cur.high = max(cur.high, c.high)
            cur.low = min(cur.low, c.low)
            cur.close = c.close
            cur.volume += c.volume

        if c.open_time + self.base_ms >= bucket + self.target_ms:
            if not self._cur_partial:
                done.append(self._cur)
            self._cur = None
            self._cur_partial = False
        return done
