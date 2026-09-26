import math
import random

import pytest

from bot.updown.config import UpDownConfig
from bot.updown.fees import FeeModel
from bot.updown.mathutil import ceil_to_tick, floor_to_tick, norm_cdf, student_t_cdf, unit_variance_t_cdf
from bot.updown.model import (
    Nowcaster,
    annual_to_per_second,
    estimate_vol,
    future_average_variance_units,
    prob_at_least,
    settlement_distribution,
)
from bot.updown.series import PriceSeries
from updown_helpers import snapshot


# -- math ------------------------------------------------------------------------
def test_student_t_matches_closed_forms():
    for x in (-3.0, -0.7, 0.0, 0.4, 2.5):
        cauchy = 0.5 + math.atan(x) / math.pi  # dof = 1
        assert student_t_cdf(x, 1.0) == pytest.approx(cauchy, abs=1e-9)
        dof2 = 0.5 + x / (2 * math.sqrt(2 + x * x))  # dof = 2
        assert student_t_cdf(x, 2.0) == pytest.approx(dof2, abs=1e-9)


def test_t_converges_to_normal_and_has_fatter_tails():
    assert unit_variance_t_cdf(1.3, 400.0) == pytest.approx(norm_cdf(1.3), abs=2e-3)
    # same variance, more tail mass: far out-of-the-money is less certain
    assert 1 - unit_variance_t_cdf(3.0, 5.0) > 1 - norm_cdf(3.0)


def test_tick_rounding():
    assert floor_to_tick(0.987, 0.01) == 0.98
    assert floor_to_tick(0.985, 0.001) == 0.985
    assert ceil_to_tick(0.981, 0.01) == 0.99
    assert floor_to_tick(0.57, 0.01) == 0.57  # no float drift down a tick


def test_fee_curve():
    fees = FeeModel()
    assert fees.taker_per_share(0.5) == pytest.approx(0.5 * 0.25 * 0.25**2)  # 1.5625% of notional
    assert fees.taker_per_share(0.98) < 0.0002
    assert fees.taker_per_share(0.0) == 0.0 and fees.maker_per_share(0.5) == 0.0


# -- series ---------------------------------------------------------------------------
def test_series_asof_sampling_and_gaps():
    s = PriceSeries()
    assert s.update(100.2, 10.0)
    assert s.update(101.7, 11.0)
    assert not s.update(101.0, 99.0)  # out of order ignored
    assert s.update(105.1, 12.0)
    assert s.price_at(99.5) is None
    assert s.price_at(103.0) == 11.0
    # samples at 100..105 -> 10, 11, 11, 11, 11, 12
    assert s.sample_mean(100, 105) == pytest.approx((10 + 11 * 4 + 12) / 6)
    assert s.max_gap(101, 105) == pytest.approx(4.0)
    assert s.log_return(105.5, 5.0) == pytest.approx(math.log(12 / 10))


def test_realized_vol_recovers_true_sigma():
    rng = random.Random(1)
    sigma = annual_to_per_second(0.6)
    s = PriceSeries()
    p = 100.0
    for t in range(4000):
        p *= math.exp(sigma * rng.gauss(0, 1))
        s.update(1000.0 + t, p)
    cfg = UpDownConfig().model
    cfg.vol_floor_annual = {}
    cfg.default_vol_floor_annual = 0.01
    est = estimate_vol(s, None, 1000.0 + 3999, "x", cfg)
    assert est is not None
    assert est.sigma == pytest.approx(sigma, rel=0.2)


# -- settlement distribution -------------------------------------------------------------
def test_last_price_rule_is_plain_random_walk():
    d = settlement_distribution(now=0.0, end=100.0, samples=1, spot=100.0, sigma=0.001, realized_mean=None)
    assert d.sd == pytest.approx(100.0 * 0.001 * math.sqrt(100.0))


def test_future_average_variance_formula():
    # continuous limit: variance of the mean of a random walk over [0, m] is m/3
    assert future_average_variance_units(1.0, 3000) == pytest.approx(3000 / 3, rel=0.01)
    # one sample d seconds away is just d
    assert future_average_variance_units(7.0, 1) == pytest.approx(7.0)


def test_twap_before_window_matches_monte_carlo():
    rng = random.Random(7)
    sigma, spot, strike = 0.0004, 100.0, 100.05
    now, end, samples = 0.0, 120.0, 60
    d = settlement_distribution(now=now, end=end, samples=samples, spot=spot, sigma=sigma, realized_mean=None)
    p_model = prob_at_least(d, strike, "normal", 0)
    ups, n = 0, 4000
    for _ in range(n):
        x, total = spot, 0.0
        for t in range(1, int(end) + 1):
            x *= math.exp(sigma * rng.gauss(0, 1))
            if t > end - samples:
                total += x
        ups += total / samples >= strike
    assert p_model == pytest.approx(ups / n, abs=0.03)


def test_twap_inside_window_uses_realized_part():
    # samples at t=241..300; by t=280.2 the 40 samples 241..280 are realized
    d = settlement_distribution(now=280.2, end=300.0, samples=60, spot=100.0, sigma=0.0005, realized_mean=100.2)
    assert d.realized_n == 40 and d.future_n == 20
    assert d.mean == pytest.approx((40 * 100.2 + 20 * 100.0) / 60)
    assert prob_at_least(d, 100.0, "normal", 0) > 0.99


def test_fully_realized_is_deterministic():
    d = settlement_distribution(now=301.0, end=300.0, samples=60, spot=90.0, sigma=0.01, realized_mean=100.0)
    assert d.sd == 0.0
    assert prob_at_least(d, 100.0, "normal", 0) == 1.0  # ties resolve Up
    assert prob_at_least(d, 100.01, "normal", 0) == 0.0


def test_probability_properties():
    at_money = snapshot(delta=0.0, remaining=200)
    assert at_money.p_up == pytest.approx(0.5, abs=1e-9)
    up_a = snapshot(delta=0.001, remaining=200).p_up
    up_b = snapshot(delta=0.002, remaining=200).p_up
    assert 0.5 < up_a < up_b  # monotonic in distance
    # same lead, less time -> more certain
    assert snapshot(delta=0.001, remaining=60).p_up > snapshot(delta=0.001, remaining=240).p_up
    # vol band brackets the point estimate
    s = snapshot(delta=0.001, remaining=120)
    assert s.p_up_lo <= s.p_up <= s.p_up_hi
    assert s.p_conservative("Up") == s.p_up_lo
    assert s.p_conservative("Down") == pytest.approx(1 - s.p_up_hi)


def test_nowcaster_tracks_basis():
    n = Nowcaster(halflife_s=10)
    for t in range(100):
        n.observe(float(t), 101.0, 100.0)  # oracle trades 1% over the CEX
    assert n.nowcast(200.0) == pytest.approx(202.0, rel=1e-6)
