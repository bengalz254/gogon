"""Per-second price history for one asset from one source (oracle or CEX).

Prices are stored as a step function bucketed by whole second: each bucket
holds the last price seen during that second, and it stays in effect until
the next bucket. That matches how the settlement TWAP proxy is sampled
("one sample per second") and keeps memory bounded even for chatty CEX
trade streams.
"""
from __future__ import annotations

import math
from bisect import bisect_right


class PriceSeries:
    def __init__(self, max_age_s: float = 2 * 3600):
        self.max_age_s = max_age_s
        self._secs: list[int] = []
        self._px: list[float] = []
        self.last_ts: float | None = None
        self.last_price: float | None = None

    def __len__(self) -> int:
        return len(self._secs)

    # -- ingest -------------------------------------------------------------
    def update(self, ts: float, price: float) -> bool:
        """Add a tick. Returns False (and ignores it) if invalid or out of order."""
        if price is None or not math.isfinite(price) or price <= 0:
            return False
        if self.last_ts is not None and ts < self.last_ts:
            return False
        sec = int(math.floor(ts))
        if self._secs and self._secs[-1] == sec:
            self._px[-1] = price
        else:
            self._secs.append(sec)
            self._px.append(price)
        self.last_ts = ts
        self.last_price = price
        self._prune(sec)
        return True

    def _prune(self, sec: int) -> None:
        cutoff = sec - self.max_age_s
        if self._secs and self._secs[0] < cutoff and len(self._secs) > 64:
            i = bisect_right(self._secs, cutoff)
            # Keep one bucket at/before the cutoff so as-of lookups still work.
            i = max(0, i - 1)
            if i > 0:
                del self._secs[:i]
                del self._px[:i]

    # -- queries ------------------------------------------------------------
    @property
    def first_ts(self) -> float | None:
        return float(self._secs[0]) if self._secs else None

    def age(self, now: float) -> float:
        return math.inf if self.last_ts is None else max(0.0, now - self.last_ts)

    def covers(self, since: float) -> bool:
        return bool(self._secs) and self._secs[0] <= since

    def price_at(self, ts: float) -> float | None:
        """As-of price: the latest bucket at or before second floor(ts)."""
        if not self._secs:
            return None
        i = bisect_right(self._secs, int(math.floor(ts))) - 1
        if i < 0:
            return None
        return self._px[i]

    def first_price_at_or_after(self, ts: float, within_s: float) -> tuple[float, float] | None:
        """(sec, price) of the first bucket in [floor(ts), ts + within_s]."""
        if not self._secs:
            return None
        sec = int(math.floor(ts))
        i = bisect_right(self._secs, sec - 1)
        if i < len(self._secs) and self._secs[i] <= ts + within_s:
            return float(self._secs[i]), self._px[i]
        return None

    def last_bucket_at_or_before(self, ts: float) -> tuple[float, float] | None:
        if not self._secs:
            return None
        i = bisect_right(self._secs, int(math.floor(ts))) - 1
        if i < 0:
            return None
        return float(self._secs[i]), self._px[i]

    def sample_mean(self, first_sec: int, last_sec: int) -> float | None:
        """Mean of as-of prices sampled at every integer second in
        [first_sec, last_sec] (inclusive). None if no data at first_sec."""
        if last_sec < first_sec or not self._secs:
            return None
        i = bisect_right(self._secs, first_sec) - 1
        if i < 0:
            return None
        n = len(self._secs)
        total = 0.0
        count = 0
        price = self._px[i]
        for s in range(first_sec, last_sec + 1):
            while i + 1 < n and self._secs[i + 1] <= s:
                i += 1
                price = self._px[i]
            total += price
            count += 1
        return total / count

    def max_gap(self, t0: float, t1: float) -> float:
        """Longest stretch in [t0, t1] without a new bucket (seconds)."""
        if not self._secs:
            return math.inf
        start = bisect_right(self._secs, int(math.floor(t0)))
        prev = self.last_bucket_at_or_before(t0)
        if prev is None:
            return math.inf
        # Measure from the bucket in effect at t0, so staleness carried into
        # the range counts as part of the gap.
        last = prev[0]
        gap = 0.0
        for sec in self._secs[start:]:
            if sec > t1:
                break
            gap = max(gap, sec - last)
            last = float(sec)
        return max(gap, t1 - last)

    def sampled_log_returns(self, now: float, lookback_s: float, step_s: float) -> list[float]:
        """Log returns between as-of prices sampled every `step_s` seconds
        over the trailing `lookback_s` window (clipped to available data)."""
        if not self._secs or step_s <= 0:
            return []
        start = max(float(self._secs[0]), now - lookback_s)
        n_steps = int((now - start) // step_s)
        if n_steps < 1:
            return []
        rets: list[float] = []
        prev = self.price_at(now - n_steps * step_s)
        for k in range(n_steps - 1, -1, -1):
            cur = self.price_at(now - k * step_s)
            if prev is not None and cur is not None and prev > 0 and cur > 0:
                rets.append(math.log(cur / prev))
            prev = cur
        return rets

    def log_return(self, now: float, lookback_s: float) -> float | None:
        """log(price_now / price_{now-lookback}); None if history is too short."""
        if not self.covers(now - lookback_s):
            return None
        p0 = self.price_at(now - lookback_s)
        p1 = self.price_at(now)
        if not p0 or not p1:
            return None
        return math.log(p1 / p0)
