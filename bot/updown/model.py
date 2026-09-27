"""Settlement-aware fair-value model: P(Up) for one Up/Down window.

The market resolves Up when the settlement price S >= price_to_beat (K).
With the "twap" rule, S is the mean of W per-second oracle samples taken at
T-W+1 ... T (T = window end); with the "last" rule W = 1 (just the price at T).

Model: from the effective spot price s (oracle, nudged by a CEX lead), the
log price is a driftless random walk with volatility sigma per sqrt(second).
Some settlement samples may already be realized (inside the TWAP window);
the rest are future. For m future samples at t = d, d+1, ..., d+m-1 seconds
from now, their average has variance

    sigma^2 * s^2 * [ (d - 1) + (m + 1)(2m + 1) / (6m) ]

(sum of min(t_i, t_j) over all pairs / m^2). With R realized samples of
mean r, S ~ (R*r + m*s) / (R + m) +/- (m / (R + m)) * that std dev, and
P(Up) = F((E[S] - K) / sd(S)), where F is a unit-variance Student-t
(fat tails) or the normal CDF.

Everything here is pure math, no IO: easy to unit test and to reuse in the
backtester and simulator.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from bot.updown.config import ModelConfig
from bot.updown.mathutil import norm_cdf, unit_variance_t_cdf
from bot.updown.series import PriceSeries

SECONDS_PER_YEAR = 365.0 * 24 * 3600


def annual_to_per_second(vol_annual: float) -> float:
    return vol_annual / math.sqrt(SECONDS_PER_YEAR)


def per_second_to_annual(sigma: float) -> float:
    return sigma * math.sqrt(SECONDS_PER_YEAR)


# --------------------------------------------------------------------------
# Volatility
# --------------------------------------------------------------------------
@dataclass
class VolEstimate:
    sigma: float  # per sqrt(second), used by the model (>= floor)
    sigma_short: float | None
    sigma_long: float | None
    floor: float
    n_short: int
    n_long: int
    source: str

    @property
    def spike_ratio(self) -> float | None:
        if self.sigma_short is None or not self.sigma_long:
            return None
        return self.sigma_short / self.sigma_long


def realized_sigma(series: PriceSeries, now: float, lookback_s: float, step_s: float) -> tuple[float | None, int]:
    rets = series.sampled_log_returns(now, lookback_s, step_s)
    if len(rets) < 2:
        return None, len(rets)
    var_per_s = sum(r * r for r in rets) / (len(rets) * step_s)
    return math.sqrt(var_per_s), len(rets)


def estimate_vol(
    oracle: PriceSeries, cex: PriceSeries | None, now: float, asset: str, cfg: ModelConfig
) -> VolEstimate | None:
    """Blend short- and long-horizon realized vol; never below the asset floor.

    Returns None when there isn't enough history to trust any estimate.
    """
    def pick(horizon: float) -> tuple[PriceSeries, str]:
        """Series to use for one lookback horizon: the settlement source when
        it has the history, else the CEX (e.g. right after startup + kline
        bootstrap)."""
        if cfg.vol_source == "cex" and cex is not None:
            return cex, "cex"
        if cfg.vol_source == "auto" and cex is not None and not oracle.covers(now - horizon) \
                and cex.covers(now - horizon):
            return cex, "cex"
        return oracle, "oracle"

    short_series, short_src = pick(cfg.vol_short_s)
    long_series, long_src = pick(cfg.vol_long_s)
    source = short_src if short_src == long_src else f"{short_src}+{long_src}"
    s_short, n_short = realized_sigma(short_series, now, cfg.vol_short_s, cfg.vol_sample_s)
    s_long, n_long = realized_sigma(long_series, now, cfg.vol_long_s, cfg.vol_sample_s)
    if n_short < cfg.min_vol_samples:
        s_short = None
    if n_long < cfg.min_vol_samples:
        s_long = None

    if s_short is None and s_long is None:
        return None
    if s_short is None:
        blended = s_long
    elif s_long is None:
        blended = s_short
    else:
        w = cfg.short_weight
        blended = math.sqrt(w * s_short**2 + (1 - w) * s_long**2)

    floor = annual_to_per_second(cfg.vol_floor(asset))
    return VolEstimate(
        sigma=max(blended, floor),
        sigma_short=s_short,
        sigma_long=s_long,
        floor=floor,
        n_short=n_short,
        n_long=n_long,
        source=source,
    )


# --------------------------------------------------------------------------
# CEX lead ("nowcast" of the oracle)
# --------------------------------------------------------------------------
class Nowcaster:
    """Tracks the oracle/CEX price ratio (EMA) so a fresh CEX print can be
    translated into "where the oracle is about to be". Used only to project
    forward -- never as the settlement price itself."""

    def __init__(self, halflife_s: float):
        self.halflife_s = max(1e-6, halflife_s)
        self.ratio: float | None = None
        self._last_ts: float | None = None

    def observe(self, ts: float, oracle_price: float, cex_price: float | None) -> None:
        if not cex_price or cex_price <= 0 or oracle_price <= 0:
            return
        r = oracle_price / cex_price
        if self.ratio is None or self._last_ts is None:
            self.ratio = r
        else:
            dt = max(0.0, ts - self._last_ts)
            alpha = 1.0 - math.exp(-dt * math.log(2) / self.halflife_s)
            self.ratio += alpha * (r - self.ratio)
        self._last_ts = ts

    def nowcast(self, cex_price: float) -> float | None:
        return None if self.ratio is None else cex_price * self.ratio


# --------------------------------------------------------------------------
# Settlement distribution and P(Up)
# --------------------------------------------------------------------------
@dataclass
class SettlementDistribution:
    mean: float
    sd: float
    realized_n: int
    future_n: int
    realized_mean: float | None


def future_average_variance_units(d: float, m: int) -> float:
    """Variance (in sigma^2 * seconds) of the mean of a random walk sampled
    at d, d+1, ..., d+m-1 seconds from now."""
    if m <= 0:
        return 0.0
    return max(0.0, (d - 1.0) + (m + 1.0) * (2.0 * m + 1.0) / (6.0 * m))


def settlement_distribution(
    *,
    now: float,
    end: float,
    samples: int,
    spot: float,
    sigma: float,
    realized_mean: float | None,
    drift: float = 0.0,
) -> SettlementDistribution:
    """Distribution of the settlement value.

    samples: number of per-second samples averaged (1 = last-price rule).
    realized_mean: mean of the settlement samples already observed (needed
        once now > end - samples; ignored before that).
    drift: expected relative drift per second (0 unless a strategy injects
        a view, e.g. constellation catch-up).
    """
    first_sample = int(end) - samples + 1
    realized_n = min(samples, max(0, int(math.floor(now)) - first_sample + 1))
    future_n = samples - realized_n
    if realized_n > 0 and realized_mean is None:
        raise ValueError("realized_mean required once the settlement window has started")

    if future_n == 0:
        return SettlementDistribution(realized_mean, 0.0, realized_n, 0, realized_mean)

    next_sample = first_sample + realized_n
    d = max(1e-6, next_sample - now)
    # Mean time (from now) of the future samples, for the drift term.
    mean_t = d + (future_n - 1) / 2.0
    future_mean = spot * (1.0 + drift * mean_t)
    var_units = future_average_variance_units(d, future_n)
    future_sd = spot * sigma * math.sqrt(var_units)

    total = realized_n + future_n
    if realized_n:
        mean = (realized_n * realized_mean + future_n * future_mean) / total
    else:
        mean = future_mean
    sd = future_sd * future_n / total
    return SettlementDistribution(mean, sd, realized_n, future_n, realized_mean)


def prob_at_least(dist: SettlementDistribution, strike: float, distribution: str, dof: float) -> float:
    """P(settlement >= strike). Ties resolve Up."""
    if dist.sd <= 0:
        return 1.0 if dist.mean >= strike else 0.0
    z = (dist.mean - strike) / dist.sd
    if distribution == "student_t":
        return unit_variance_t_cdf(z, dof)
    return norm_cdf(z)


@dataclass
class ModelSnapshot:
    """Everything the strategies need about one window at one instant."""

    ts: float
    elapsed: float
    remaining: float
    ptb: float
    oracle_price: float
    oracle_age: float
    spot: float  # effective spot (oracle nudged by the CEX lead)
    cex_price: float | None
    delta: float  # spot / ptb - 1
    sigma: float
    vol: VolEstimate
    realized_mean: float | None
    realized_n: int
    future_n: int
    settle_mean: float
    settle_sd: float
    z: float
    p_up: float
    p_up_lo: float  # min over the vol-uncertainty band
    p_up_hi: float  # max over the vol-uncertainty band

    def p_model(self, outcome: str) -> float:
        return self.p_up if outcome == "Up" else 1.0 - self.p_up

    def p_conservative(self, outcome: str) -> float:
        """Lowest probability for `outcome` across the vol band."""
        return self.p_up_lo if outcome == "Up" else 1.0 - self.p_up_hi


def evaluate_window(
    *,
    now: float,
    start: float,
    end: float,
    samples: int,
    ptb: float,
    spot: float,
    oracle_price: float,
    oracle_age: float,
    cex_price: float | None,
    vol: VolEstimate,
    realized_mean: float | None,
    cfg: ModelConfig,
    drift: float = 0.0,
) -> ModelSnapshot:
    def p_for(sigma: float) -> tuple[float, SettlementDistribution]:
        dist = settlement_distribution(
            now=now, end=end, samples=samples, spot=spot, sigma=sigma,
            realized_mean=realized_mean, drift=drift,
        )
        return prob_at_least(dist, ptb, cfg.distribution, cfg.tail_dof), dist

    p_mid, dist = p_for(vol.sigma)
    u = cfg.vol_uncertainty
    p_a, _ = p_for(vol.sigma * (1.0 - u))
    p_b, _ = p_for(vol.sigma * (1.0 + u))
    z = (dist.mean - ptb) / dist.sd if dist.sd > 0 else (math.inf if dist.mean >= ptb else -math.inf)
    return ModelSnapshot(
        ts=now,
        elapsed=now - start,
        remaining=max(0.0, end - now),
        ptb=ptb,
        oracle_price=oracle_price,
        oracle_age=oracle_age,
        spot=spot,
        cex_price=cex_price,
        delta=spot / ptb - 1.0,
        sigma=vol.sigma,
        vol=vol,
        realized_mean=realized_mean,
        realized_n=dist.realized_n,
        future_n=dist.future_n,
        settle_mean=dist.mean,
        settle_sd=dist.sd,
        z=z,
        p_up=p_mid,
        p_up_lo=min(p_mid, p_a, p_b),
        p_up_hi=max(p_mid, p_a, p_b),
    )


def settlement_value(series: PriceSeries, end: float, samples: int) -> float | None:
    """Settlement price computed from recorded oracle data (mean of the last
    `samples` per-second as-of prices up to `end`)."""
    last_sec = int(end)
    return series.sample_mean(last_sec - samples + 1, last_sec)
