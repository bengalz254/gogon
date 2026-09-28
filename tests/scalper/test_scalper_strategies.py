import pytest
from fakes import ranging_sequence, trend_pullback_sequence

from scalper.config import Settings
from scalper.models import LONG, SHORT
from scalper.strategies import RangeReversionStrategy, TrendPullbackStrategy, build_strategy


def run(strategy, candles):
    return [(i, s) for i, c in enumerate(candles) if (s := strategy.on_candle(c)) is not None]


@pytest.mark.parametrize("sign, side", [(1, LONG), (-1, SHORT)])
def test_trend_pullback_fires_once_on_the_resumption_bar(sign, side):
    candles = trend_pullback_sequence(sign)
    signals = run(build_strategy(Settings(), "BTCUSDT"), candles)
    assert [i for i, _ in signals] == [len(candles) - 1]
    sig = signals[0][1]
    last = candles[-1]
    assert sig.side == side
    assert sig.entry_ref == last.close
    assert sig.time == last.close_time
    assert sig.tp_r == 1.5
    risk = abs(sig.entry_ref - sig.stop)
    assert risk >= 0.8 * sig.atr - 1e-9  # min_sl_atr respected
    assert risk <= 2.5 * sig.atr + 1e-9  # max_sl_atr respected
    if side == LONG:
        assert sig.stop < min(c.low for c in candles[-6:])
        assert sig.take_profit == pytest.approx(sig.entry_ref + 1.5 * risk)
    else:
        assert sig.stop > max(c.high for c in candles[-6:])
        assert sig.take_profit == pytest.approx(sig.entry_ref - 1.5 * risk)


def test_trend_pullback_respects_direction_switches():
    s = Settings()
    s.strategy.allow_long = False
    strat = build_strategy(s, "BTCUSDT")
    assert run(strat, trend_pullback_sequence(1)) == []
    assert "disabled" in strat.last_skip_reason


def test_trend_pullback_needs_higher_timeframe_agreement():
    # Same local pattern, but the higher timeframe filter must agree: feeding a
    # long setup to a strategy whose HTF EMAs point down produces nothing.
    s = Settings()
    strat = build_strategy(s, "BTCUSDT")
    candles = trend_pullback_sequence(1)
    for c in candles[:-1]:
        strat.on_candle(c)
    strat.htf_fast.value, strat.htf_slow.value = 50.0, 60.0  # force a downtrend reading
    assert strat.on_candle(candles[-1]) is None
    assert "trend" in strat.last_skip_reason


def test_trend_pullback_warmup_and_volatility_filter():
    s = Settings()
    strat = build_strategy(s, "BTCUSDT")
    assert strat.warmup_bars == 12 * (50 + 2)
    s.strategy.trend_pullback.min_atr_pct = 5.0  # impossible volatility floor
    assert run(build_strategy(s, "BTCUSDT"), trend_pullback_sequence(1)) == []


def test_duplicate_candles_are_ignored():
    strat = build_strategy(Settings(), "BTCUSDT")
    candles = trend_pullback_sequence(1)
    for c in candles[:-1]:
        strat.on_candle(c)
    bars = strat.bars_seen
    strat.on_candle(candles[-2])  # replayed candle
    assert strat.bars_seen == bars


@pytest.mark.parametrize("sign, side", [(1, LONG), (-1, SHORT)])
def test_range_reversion_fires_on_band_reentry(sign, side):
    s = Settings()
    s.strategy.name = "range_reversion"
    strat = build_strategy(s, "BTCUSDT")
    assert isinstance(strat, RangeReversionStrategy)
    candles = ranging_sequence(sign)
    signals = run(strat, candles)
    assert [i for i, _ in signals] == [len(candles) - 1]
    sig = signals[0][1]
    assert sig.side == side and sig.tp_r is None
    assert sig.take_profit == pytest.approx(strat.bb.mid)
    reward = abs(sig.take_profit - sig.entry_ref)
    risk = abs(sig.entry_ref - sig.stop)
    assert reward / risk >= 1.0
    assert risk >= 1.0 * sig.atr - 1e-9


def test_range_reversion_skips_trends():
    s = Settings()
    s.strategy.name = "range_reversion"
    assert run(build_strategy(s, "BTCUSDT"), trend_pullback_sequence(1)) == []


def test_build_strategy_types():
    assert isinstance(build_strategy(Settings(), "X"), TrendPullbackStrategy)
