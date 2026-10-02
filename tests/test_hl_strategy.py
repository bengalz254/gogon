from hlbot.indicators import ema
from hlbot.strategy import LONG, SHORT, Candle, closed_candles, cross_at, latest_signal

from hl_helpers import SPAN_30M, T0, down_then_up


def test_ema_seeded_with_sma_and_none_before_period():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = ema(vals, 3)
    assert out[:2] == [None, None]
    assert out[2] == 2.0  # SMA of 1,2,3
    k = 2 / 4
    assert abs(out[3] - (4 * k + 2.0 * (1 - k))) < 1e-12
    assert abs(out[4] - (5 * k + out[3] * (1 - k))) < 1e-12


def test_ema_short_input_is_all_none():
    assert ema([1.0, 2.0], 3) == [None, None]


def test_cross_up_and_down():
    fast = [1.0, 1.0, 3.0, 3.0, 1.0]
    slow = [2.0, 2.0, 2.0, 2.0, 2.0]
    assert cross_at(fast, slow, 1) is None
    assert cross_at(fast, slow, 2) == LONG
    assert cross_at(fast, slow, 3) is None
    assert cross_at(fast, slow, 4) == SHORT


def test_cross_after_touch_counts_once():
    fast = [1.0, 2.0, 3.0]
    slow = [2.0, 2.0, 2.0]
    assert cross_at(fast, slow, 1) is None  # touching is not a cross yet
    assert cross_at(fast, slow, 2) == LONG


def test_cross_ignores_missing_values():
    assert cross_at([None, 3.0], [2.0, 2.0], 1) is None


def test_latest_signal_only_fires_on_the_cross_candle():
    closes = down_then_up()
    signals = [latest_signal(closes[: i + 1], 9, 21) for i in range(len(closes))]
    longs = [i for i, s in enumerate(signals) if s == LONG]
    assert len(longs) == 1
    assert all(s is None for i, s in enumerate(signals) if i != longs[0])
    assert longs[0] > 40  # after the bottom


def test_closed_candles_drops_forming_candle():
    candles = [Candle(t=T0 + i * SPAN_30M, o=1, h=1, l=1, c=1) for i in range(3)]
    now = T0 + 2 * SPAN_30M + 60_000  # one minute into the 3rd candle
    assert [c.t for c in closed_candles(candles, "30m", now)] == [T0, T0 + SPAN_30M]
    # exactly at the boundary the 3rd candle is closed
    assert len(closed_candles(candles, "30m", T0 + 3 * SPAN_30M)) == 3
