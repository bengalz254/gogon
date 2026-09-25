"""Pricing model for Polymarket 5-minute "Up or Down" markets.

Pure math, no network or IO, so it's easy to test and to reason about.

A 5-minute market resolves "Up" if the reference price (Chainlink BTC/USD) at
the end of the window is >= the price at the start of the window (the
"price to beat", K). Midway through the window we know the current spot
price S and the time left tau, so the fair probability of "Up" is roughly:

    P(Up) = Phi( ln(S/K) / sqrt(sigma^2 * tau + basis_sd^2) )

- sigma is short-horizon volatility per sqrt(second), estimated live.
- basis_sd covers the gap between our feed (e.g. Binance) and the oracle
  that actually settles the market (Chainlink). Near expiry, when
  sigma^2*tau is tiny, this term stops the model from being overconfident
  about a price that sits a few dollars from the line.

Drift is ignored on purpose: over a few minutes it is tiny compared with
volatility, and guessing it is how most "predict the next candle" bots lose.
"""
from __future__ import annotations

import math


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_prob_up(
    spot: float,
    strike: float,
    seconds_left: float,
    sigma_per_sqrt_s: float,
    basis_sd: float = 0.0,
    vol_multiplier: float = 1.0,
) -> float:
    """Probability that the price at expiry is >= strike."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    tau = max(0.0, seconds_left)
    sigma = max(0.0, sigma_per_sqrt_s) * vol_multiplier
    total_sd = math.sqrt(sigma * sigma * tau + basis_sd * basis_sd)
    log_move = math.log(spot / strike)
    if total_sd <= 0.0:
        return 1.0 if log_move >= 0 else 0.0
    return norm_cdf(log_move / total_sd)


def prob_vol_1s(spot: float, strike: float, seconds_left: float, sigma_per_sqrt_s: float) -> float:
    """How much P(Up) moves (1 sd) in one second.

    dP/dln(S) = pdf(z) / (sigma * sqrt(tau)), and ln(S) moves sigma per
    sqrt(second), so one second moves P by pdf(z) / sqrt(tau): largest near
    the strike and near the close.
    """
    tau = max(seconds_left, 1.0)
    if spot <= 0 or strike <= 0 or sigma_per_sqrt_s <= 0:
        return 0.0
    z = math.log(spot / strike) / (sigma_per_sqrt_s * math.sqrt(tau))
    return math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi) / math.sqrt(tau)


def taker_fee_per_share(price: float, fee_rate: float, fee_exponent: float) -> float:
    """Taker fee in USDC per share bought or sold at `price`.

    Polymarket charges takers on short-term crypto markets a fee that's
    largest at 50c and shrinks toward 0c/100c. The shape used here is
    fee_rate * (p * (1 - p)) ** fee_exponent. Check the current numbers in
    Polymarket's fee docs and set them in config/updown.yaml.
    """
    p = min(max(price, 0.0), 1.0)
    if fee_rate <= 0:
        return 0.0
    return fee_rate * (p * (1.0 - p)) ** fee_exponent


def kelly_fraction(win_prob: float, cost_per_share: float) -> float:
    """Kelly bet fraction for a share that costs `cost_per_share` and pays $1 on a win.

    f* = (p - c) / (1 - c). Returns 0 when there's no positive edge.
    """
    if cost_per_share <= 0 or cost_per_share >= 1:
        return 0.0
    f = (win_prob - cost_per_share) / (1.0 - cost_per_share)
    return max(0.0, f)


class VolEstimator:
    """EWMA estimate of volatility per sqrt(second) from irregular price samples.

    Each squared log return is divided by its time gap, so ticks that arrive
    unevenly (1s, then 3s after a hiccup) still add up correctly. The
    estimate is clamped to [floor, cap] so one bad tick can't make the model
    wildly over- or under-confident.
    """

    def __init__(self, halflife_s: float, floor: float, cap: float, initial: float | None = None):
        if halflife_s <= 0:
            raise ValueError("halflife_s must be positive")
        self.halflife_s = halflife_s
        self.floor = floor
        self.cap = cap
        self._var: float | None = initial * initial if initial else None
        self._last_price: float | None = None
        self._last_ts: float | None = None

    def seed(self, sigma_per_sqrt_s: float) -> None:
        self._var = sigma_per_sqrt_s * sigma_per_sqrt_s

    def update(self, ts: float, price: float) -> None:
        if price <= 0:
            return
        if self._last_price is not None and self._last_ts is not None:
            dt = ts - self._last_ts
            if dt <= 0:
                return
            # A gap of more than a minute means the feed stalled; one big
            # return over that gap is still valid, but longer gaps say little
            # about current conditions, so just restart from here.
            if dt <= 60.0:
                r = math.log(price / self._last_price)
                sample_var = (r * r) / dt
                w = 1.0 - 0.5 ** (dt / self.halflife_s)
                self._var = sample_var if self._var is None else (1 - w) * self._var + w * sample_var
        self._last_price = price
        self._last_ts = ts

    @property
    def sigma(self) -> float:
        if self._var is None:
            return self.floor
        return min(self.cap, max(self.floor, math.sqrt(self._var)))


def sigma_from_closes(closes: list[float], interval_s: float) -> float | None:
    """Volatility per sqrt(second) from evenly spaced closes (e.g. 1m candles)."""
    if len(closes) < 3 or interval_s <= 0:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    var = sum(r * r for r in rets) / len(rets)
    return math.sqrt(var / interval_s)
