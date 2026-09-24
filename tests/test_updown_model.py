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
