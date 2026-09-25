import math
import random

import pytest

from scripts.updown_calibrate import evaluate
from updown.config import ModelConfig
from updown.model import VolEstimator, fair_prob_up, kelly_fraction, sigma_from_closes, taker_fee_per_share


def test_at_the_money_is_coin_flip():
    assert fair_prob_up(100.0, 100.0, 120, 1e-4) == pytest.approx(0.5)


def test_prob_rises_with_price_and_as_time_runs_out():
    early = fair_prob_up(100.1, 100.0, 240, 1e-4)
    late = fair_prob_up(100.1, 100.0, 30, 1e-4)
    assert 0.5 < early < late < 1.0
    assert fair_prob_up(100.0 / 1.001, 100.0, 30, 1e-4) == pytest.approx(1 - late, abs=1e-9)


def test_basis_keeps_model_humble_at_expiry():
    # One tick above the line with 0s left: certain without basis, not with it.
    assert fair_prob_up(100.001, 100.0, 0, 1e-4) == 1.0
    assert 0.5 < fair_prob_up(100.001, 100.0, 0, 1e-4, basis_sd=0.0002) < 0.6


def test_fee_is_largest_at_50c_and_symmetric():
    f50 = taker_fee_per_share(0.5, 0.25, 2)
    assert f50 == pytest.approx(0.25 * 0.0625)
    assert taker_fee_per_share(0.1, 0.25, 2) == pytest.approx(taker_fee_per_share(0.9, 0.25, 2))
    assert taker_fee_per_share(0.1, 0.25, 2) < f50
    assert taker_fee_per_share(0.5, 0.0, 2) == 0.0


def test_kelly():
    assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)
    assert kelly_fraction(0.4, 0.5) == 0.0
    assert kelly_fraction(0.9, 1.0) == 0.0


def test_vol_estimator_recovers_true_sigma_and_respects_clamps():
    rng = random.Random(1)
    true_sigma = 1e-4
    est = VolEstimator(halflife_s=600, floor=1e-6, cap=1e-2)
    price, t = 100.0, 0.0
    for _ in range(20000):
        dt = rng.choice([0.5, 1.0, 2.0])  # irregular ticks
        price *= math.exp(true_sigma * math.sqrt(dt) * rng.gauss(0, 1))
        t += dt
        est.update(t, price)
    assert est.sigma == pytest.approx(true_sigma, rel=0.15)

    clamped = VolEstimator(halflife_s=60, floor=5e-4, cap=1e-3, initial=1e-5)
    assert clamped.sigma == 5e-4


def test_sigma_from_closes():
    closes = [100.0 * math.exp(0.001 * (i % 2)) for i in range(50)]  # +-0.1% per minute
    assert sigma_from_closes(closes, 60) == pytest.approx(0.001 / math.sqrt(60), rel=1e-6)
    assert sigma_from_closes([100.0], 60) is None


def test_model_is_calibrated_on_random_walk():
    """On a random walk with known vol, predicted probabilities match outcomes."""
    rng = random.Random(3)
    sigma_min = 1e-4 * math.sqrt(60)
    t, price, candles = 0, 100_000.0, []
    for _ in range(60 * 24 * 20):
        o = price
        price *= math.exp(sigma_min * rng.gauss(0, 1))
        candles.append((t, o, price))
        t += 60
    model = ModelConfig(vol_multiplier=1.0, basis_sd=0.0)
    samples = evaluate(candles, model)
    brier = sum((p - y) ** 2 for _, p, y in samples) / len(samples)
    assert brier < 0.2  # coin flip scores 0.25
    confident = [(p, y) for _, p, y in samples if p > 0.8]
    assert sum(y for _, y in confident) / len(confident) == pytest.approx(sum(p for p, _ in confident) / len(confident), abs=0.04)


# -- TWAP settlement (Polymarket since Aug 2026) ------------------------------
from updown.model import fair_prob_up_twap, prob_vol_1s, twap_distribution, twap_prob_vol_1s  # noqa: E402


def test_twap_variance_formula():
    _, sd, w = twap_distribution(100.0, 240, 1e-4, 60)
    assert sd == pytest.approx(1e-4 * math.sqrt(240 - 40)) and w == 1.0
    exp, sd, w = twap_distribution(101.0, 30, 1e-4, 60, observed_avg=100.0)
    assert exp == pytest.approx(100.5) and w == pytest.approx(0.5)
    assert sd == pytest.approx(1e-4 * math.sqrt(10) * 0.5)
    # window 0 = the old single-price settlement
    assert twap_distribution(100.0, 240, 1e-4, 0)[1] == pytest.approx(1e-4 * math.sqrt(240))


def _simulate(seconds_left, n=20000, sigma=2e-4, w=60, seed=5):
    """Random walks from a given point; returns (spot, observed avg, outcomes)."""
    rng = random.Random(seed)
    cases = []
    for _ in range(n):
        # history: the part of the closing window already seen (if any)
        seen = max(0, w - seconds_left)
        path, p = [], 100.0
        for _ in range(seen):
            p *= math.exp(sigma * rng.gauss(0, 1))
            path.append(p)
        spot = p
        observed = sum(path) / len(path) if path else None
        future = []
        for _ in range(seconds_left):
            p *= math.exp(sigma * rng.gauss(0, 1))
            future.append(p)
        closing = (path + future)[-w:]
        cases.append((spot, observed, sum(closing) / len(closing)))
    return cases


@pytest.mark.parametrize("seconds_left", [150, 60, 20])
def test_twap_model_is_calibrated(seconds_left):
    sigma, strike = 2e-4, 100.05
    cases = _simulate(seconds_left, sigma=sigma)
    buckets = {}
    for spot, observed, closing in cases:
        p = fair_prob_up_twap(spot, strike, seconds_left, sigma, 60, observed_avg=observed)
        b = min(4, int(p * 5))
        buckets.setdefault(b, []).append((p, closing >= strike))
    for rows in buckets.values():
        if len(rows) < 800:
            continue
        predicted = sum(p for p, _ in rows) / len(rows)
        actual = sum(y for _, y in rows) / len(rows)
        assert actual == pytest.approx(predicted, abs=0.03)


def test_twap_last_minute_moves_faster_near_the_strike():
    # Inside the closing window most of the average is already locked in, so
    # what's left is tiny and every tick matters more: sqrt(3) x a snapshot market.
    snap = prob_vol_1s(100.0, 100.0, 20, 1e-4)
    twap = twap_prob_vol_1s(100.0, 100.0, 20, 1e-4, 60, observed_avg=100.0)
    assert twap == pytest.approx(math.sqrt(3) * snap, rel=1e-6)
    # Earlier too: averaging leaves less uncertainty (tau - 40 instead of tau),
    # so P(Up) is a bit MORE sensitive to each move, never less.
    assert twap_prob_vol_1s(100.0, 100.0, 200, 1e-4, 60) == pytest.approx(
        prob_vol_1s(100.0, 100.0, 200, 1e-4) * math.sqrt(200 / 160), rel=1e-6)
