import math
import random

import pytest

from scalper.indicators import ADX, ATR, EMA, RSI, SMA, Bollinger, Resampler
from scalper.models import Candle


def _series(n=300, seed=1):
    rnd = random.Random(seed)
    closes = [100.0]
    for _ in range(n):
        closes.append(closes[-1] * (1 + rnd.gauss(0, 0.01)))
    highs = [c * (1 + abs(rnd.gauss(0, 0.004))) for c in closes]
    lows = [c * (1 - abs(rnd.gauss(0, 0.004))) for c in closes]
    return closes, highs, lows


def test_ema_matches_reference_and_seeds_with_sma():
    closes, _, _ = _series()
    ema = EMA(20)
    values = [ema.update(c) for c in closes]
    assert values[18] is None and not all(v is not None for v in values[:19])
    ref = sum(closes[:20]) / 20
    assert values[19] == pytest.approx(ref)
    alpha = 2 / 21
    for c, v in zip(closes[20:], values[20:]):
        ref += alpha * (c - ref)
        assert v == pytest.approx(ref)


def test_sma():
    sma = SMA(3)
    assert [sma.update(x) for x in (1, 2, 3, 4)] == [None, None, 2.0, 3.0]


def test_rsi_matches_wilder_reference():
    closes, _, _ = _series()
    n = 14
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    ref = [100 - 100 / (1 + ag / al)]
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
        ref.append(100 - 100 / (1 + ag / al))
    rsi = RSI(n)
    values = [rsi.update(c) for c in closes]
    assert values[:n] == [None] * n
    assert values[n:] == pytest.approx(ref)


def test_rsi_extremes():
    up = RSI(5)
    for i in range(20):
        up.update(100 + i)
    assert up.value == 100.0
    flat = RSI(5)
    for _ in range(20):
        flat.update(100)
    assert flat.value == 50.0


def test_atr_matches_wilder_reference():
    closes, highs, lows = _series()
    trs = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    ref = [sum(trs[:14]) / 14]
    for tr in trs[14:]:
        ref.append((ref[-1] * 13 + tr) / 14)
    atr = ATR(14)
    values = [atr.update(h, l, c) for h, l, c in zip(highs, lows, closes)]
    assert values[13:] == pytest.approx(ref)


def test_adx_detects_trend_and_direction():
    up = ADX(14)
    for i in range(80):
        p = 100 + i + (0.3 if i % 2 else -0.3)
        up.update(p + 0.6, p - 0.6, p)
    assert up.value > 40 and up.plus_di > up.minus_di

    down = ADX(14)
    for i in range(80):
        p = 200 - i + (0.3 if i % 2 else -0.3)
        down.update(p + 0.6, p - 0.6, p)
    assert down.value > 40 and down.minus_di > down.plus_di


def test_adx_needs_two_periods_to_warm_up():
    adx = ADX(14)
    values = [adx.update(100 + i, 99 + i, 99.5 + i) for i in range(40)]
    first_ready = next(i for i, v in enumerate(values) if v is not None)
    assert first_ready == 2 * 14 - 1  # 1 bar for the first diff, 14 for DI, 14 for ADX


def test_bollinger_population_std():
    bb = Bollinger(5, 2.0)
    data = [1, 2, 3, 4, 5]
    for x in data:
        bb.update(x)
    mean = 3.0
    std = math.sqrt(sum((x - mean) ** 2 for x in data) / 5)
    assert bb.mid == pytest.approx(mean)
    assert bb.upper == pytest.approx(mean + 2 * std)
    assert bb.lower == pytest.approx(mean - 2 * std)


def _c(t, o, h, l, c, step=300_000):
    return Candle(t, o, h, l, c, 1.0, t + step - 1)


def test_resampler_emits_only_completed_buckets_and_drops_partial_start():
    hour, step = 3_600_000, 300_000
    rs = Resampler(step, hour)
    base = hour * 10
    out = []
    for i in range(30):  # starts at :30 of hour 10 -> that first hour is partial
        t = base + (6 + i) * step
        out += rs.update(_c(t, 1.0, 2.0 + i, 0.5, 1.5))
    assert [c.open_time for c in out] == [base + hour, base + 2 * hour]
    first = out[0]
    assert first.high == 2.0 + 17  # bars 6..17 of the sequence fall in hour 11
    assert first.close_time == base + 2 * hour - 1


def test_resampler_emits_bucket_with_missing_last_candle_when_next_bucket_starts():
    hour, step = 3_600_000, 300_000
    rs = Resampler(step, hour)
    out = []
    for i in range(11):  # hour 0 missing its last 5m candle
        out += rs.update(_c(i * step, 1, 1, 1, 1))
    assert out == []
    out += rs.update(_c(hour, 1, 1, 1, 1))
    assert len(out) == 1 and out[0].open_time == 0


def test_resampler_rejects_non_multiple():
    with pytest.raises(ValueError):
        Resampler(300_000, 400_000)
