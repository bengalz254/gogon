import pytest

from scalper.backtest import run_backtest
from scalper.cli import _apply_combo
from scalper.config import Settings, TrendFollowParams, load_settings, validate
from scalper.models import INTERVAL_MS, LONG, SHORT, Candle
from scalper.strategies import TrendFollowStrategy, build_strategy
from scalper.why import classify

H4 = INTERVAL_MS["4h"]


def bar(i: int, close: float, spread: float = 0.5, open_: float | None = None) -> Candle:
    o = close if open_ is None else open_
    return Candle(i * H4, o, max(o, close) + spread, min(o, close) - spread, close, close_time=(i + 1) * H4 - 1)


def strategy(**kw) -> TrendFollowStrategy:
    p = dict(entry_bars=5, trend_ema=10, atr_period=5, stop_atr=2.0, tp_r=0.0, min_atr_pct=0.0, max_atr_pct=100.0)
    p.update(kw)
    return TrendFollowStrategy("BTCUSDT", H4, TrendFollowParams(**p))


def feed(strat, closes, start=0):
    sig = None
    for i, c in enumerate(closes, start):
        sig = strat.on_candle(bar(i, c))
    return sig


def test_long_on_a_fresh_breakout_with_the_trend():
    s = strategy()
    assert feed(s, [100 + 0.2 * i for i in range(15)]) is None  # steady rise, no close above the channel
    sig = s.on_candle(bar(15, 104.0))
    assert sig is not None and sig.side == LONG
    assert sig.stop == pytest.approx(104.0 - 2.0 * s.atr)
    assert sig.tp_r is None and sig.take_profit == pytest.approx(3 * 104.0)  # no fixed target: trail out
    assert s.on_candle(bar(16, 104.5)) is None  # still above the channel: one signal per breakout
    assert classify(s.last_skip_reason) == "no_breakout"


def test_short_is_the_mirror_image():
    s = strategy()
    feed(s, [100 - 0.2 * i for i in range(15)])
    sig = s.on_candle(bar(15, 96.0))
    assert sig is not None and sig.side == SHORT
    assert sig.stop == pytest.approx(96.0 + 2.0 * s.atr) and 0 < sig.take_profit < 96.0


def test_breakout_against_the_trend_filter_is_skipped():
    s = strategy(trend_ema=30)
    closes = [110 - 0.3 * i for i in range(40)]
    feed(s, closes)
    assert s.on_candle(bar(40, closes[-1] + 2.0)) is None  # above the 5-bar high, still below EMA30
    assert classify(s.last_skip_reason) == "against_trend"


def test_fixed_target_when_tp_r_is_set():
    s = strategy(tp_r=4.0)
    feed(s, [100 + 0.2 * i for i in range(15)])
    sig = s.on_candle(bar(15, 104.0))
    assert sig.tp_r == 4.0 and sig.take_profit == pytest.approx(104.0 + 4.0 * 2.0 * s.atr)


def test_trend_config_is_valid_and_needs_an_exit():
    s = load_settings("config/trend.yaml", env_path="/nonexistent.env", mode_override="paper")
    assert s.strategy.name == "trend_follow" and s.timeframe == "4h" and s.data_dir == "data/trend"
    assert s.management.max_bars_in_trade == 0 and s.execution.take_profit_order == "market"
    assert isinstance(build_strategy(s, "BTCUSDT"), TrendFollowStrategy)
    s.management.trail_start_r = 0  # no fixed target AND no trailing stop: trades could never exit
    with pytest.raises(ValueError, match="trail_start_r"):
        validate(s)


def test_optimizer_grid_can_change_management_settings():
    s = Settings()
    s.strategy.name = "trend_follow"
    _apply_combo(s, {"entry_bars": 55, "mgmt_trail_atr": 4.0})
    assert s.strategy.trend_follow.entry_bars == 55 and s.management.trail_atr == 4.0


def test_backtest_rides_a_trend_and_trails_out_when_it_turns():
    s = load_settings("config/trend.yaml", env_path="/nonexistent.env", mode_override="paper")
    closes, price = [], 100.0
    for i in range(300):  # 150 bars up, 150 down, each with small pullbacks (up, up, dip)
        price *= (1.02, 1.02, 0.985)[i % 3] if i < 150 else (0.98, 0.98, 1.015)[i % 3]
        closes.append(price)
    candles = [bar(i, c, spread=c * 0.003, open_=closes[i - 1] if i else c) for i, c in enumerate(closes)]
    r = run_backtest(candles, s, "BTCUSDT")
    long_trade, short_trade = r.trades  # one per trend, no churn in between
    assert long_trade.side == LONG and long_trade.exit_reason == "TRAIL"  # trailed out when the trend turned
    assert long_trade.r_multiple > 3  # the trend paid several times the risk
    assert short_trade.side == SHORT and short_trade.r_multiple > 3  # then it rode the way down
    assert r.end_equity > r.start_equity
