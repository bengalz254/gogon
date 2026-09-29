import pytest
from fakes import ScriptedStrategy, flat, ohlc

from scalper.backtest import run_backtest
from scalper.cli import GRIDS, _apply_combo
from scalper.config import EmaCrossParams, Settings, load_settings, validate
from scalper.models import INTERVAL_MS, LONG, SHORT, Candle
from scalper.risk import fee_filter
from scalper.strategies import EmaCrossStrategy, build_strategy
from scalper.why import classify

M3 = INTERVAL_MS["3m"]
T0 = 1_700_006_400_000


def bar(i: int, close: float, spread: float = 0.1) -> Candle:
    t = T0 + i * M3
    return Candle(t, close, close + spread, close - spread, close, 100.0, t + M3 - 1)


def strategy(**kw) -> EmaCrossStrategy:
    p = dict(fast=3, slow=6, atr_period=3, sl_atr=1.5, min_sl_pct=0.0, tp_r=2.0, trend_ema=0)
    p.update(kw)
    return EmaCrossStrategy("ETHUSDT", M3, EmaCrossParams(**p))


def run(strat, closes):
    """Feed closes; return [(index, signal, skip reason)] for every candle."""
    return [(i, strat.on_candle(bar(i, c)), strat.last_skip_reason) for i, c in enumerate(closes)]


def zigzag(legs: int, n: int = 12, step: float = 0.3, start: float = 100.0) -> list[float]:
    out, p = [], start
    for leg in range(legs):
        for _ in range(n):
            p += step if leg % 2 == 0 else -step
            out.append(p)
    return out


DOWN_UP_DOWN = [100 - 0.5 * i for i in range(15)] + [93 + 0.5 * i for i in range(1, 15)] + [100 - 0.5 * i for i in range(1, 15)]


def test_cross_up_is_long_and_cross_down_is_short():
    out = run(strategy(), DOWN_UP_DOWN)
    sigs = [(i, s) for i, s, _ in out if s is not None]
    assert [s.side for _, s in sigs] == [LONG, SHORT]
    (i_long, long_), (i_short, _) = sigs
    assert 15 <= i_long < 20 and 29 <= i_short < 34  # a few bars after each turn
    assert long_.reason == "EMA3 crossed above EMA6" and long_.strategy == "ema_cross"


def test_every_candle_has_a_reason():
    out = run(strategy(), DOWN_UP_DOWN)
    assert classify(out[0][2]) == "warmup"
    codes = {classify(r) for _, s, r in out[8:] if s is None}
    assert codes == {"no_cross"}


def test_stop_is_atr_based_but_never_closer_than_min_sl_pct():
    s = strategy(sl_atr=1.5, min_sl_pct=0.0)
    sig = next(sig for _, sig, _ in run(s, DOWN_UP_DOWN) if sig is not None)
    assert sig.entry_ref - sig.stop == pytest.approx(1.5 * sig.atr)
    assert sig.take_profit - sig.entry_ref == pytest.approx(2.0 * 1.5 * sig.atr)
    assert sig.tp_r == 2.0

    s = strategy(sl_atr=1.5, min_sl_pct=5.0)  # 5% of the price is wider than 1.5 x ATR here
    sig = next(sig for _, sig, _ in run(s, DOWN_UP_DOWN) if sig is not None)
    assert sig.entry_ref - sig.stop == pytest.approx(0.05 * sig.entry_ref)
    assert sig.take_profit - sig.entry_ref == pytest.approx(2.0 * 0.05 * sig.entry_ref)


def test_short_levels_mirror_the_long_ones():
    s = strategy(min_sl_pct=5.0)
    sig = [sig for _, sig, _ in run(s, DOWN_UP_DOWN) if sig is not None][1]
    assert sig.side == SHORT
    assert sig.stop == pytest.approx(sig.entry_ref * 1.05) and sig.take_profit == pytest.approx(sig.entry_ref * 0.90)


def test_trend_filter_blocks_the_entry_but_the_cross_still_means_exit():
    s = strategy(trend_ema=30)
    closes = [130 - 0.5 * i for i in range(40)] + [110.5 + 0.5 * i for i in range(1, 6)]
    blocked = []
    for i, c in enumerate(closes):
        sig = s.on_candle(bar(i, c))
        assert sig is None
        if classify(s.last_skip_reason) == "against_trend":
            blocked.append(i)
            assert s.should_exit(SHORT) and not s.should_exit(LONG)
        else:
            assert not s.should_exit(SHORT) and not s.should_exit(LONG)
    assert len(blocked) == 1 and blocked[0] >= 40  # the bounce's cross, still under EMA30


def test_disabled_side_still_exits_the_other():
    s = EmaCrossStrategy("ETHUSDT", M3, EmaCrossParams(fast=3, slow=6, atr_period=3), allow_short=False)
    out = run(s, DOWN_UP_DOWN)
    assert [sig.side for _, sig, _ in out if sig is not None] == [LONG]
    assert s.should_exit(LONG) is False
    s2 = EmaCrossStrategy("ETHUSDT", M3, EmaCrossParams(fast=3, slow=6, atr_period=3), allow_short=False)
    for i, c in enumerate(DOWN_UP_DOWN):
        s2.on_candle(bar(i, c))
        if s2.last_skip_reason == "SHORT disabled in config":
            assert s2.should_exit(LONG)
            break
    else:
        pytest.fail("the down cross was never seen")


# -- backtest ---------------------------------------------------------------
def settings(**ema) -> Settings:
    s = Settings()
    s.symbols = ["ETHUSDT"]
    s.timeframe = "3m"
    s.strategy.name = "ema_cross"
    p = dict(fast=3, slow=6, atr_period=3, sl_atr=1.0, min_sl_pct=2.0, tp_r=2.0, trend_ema=0)
    p.update(ema)
    s.strategy.ema_cross = EmaCrossParams(**p)
    s.management.exit_on_opposite_signal = True
    s.management.cooldown_bars_after_exit = 0
    s.management.breakeven_at_r = 0
    s.management.max_bars_in_trade = 0
    s.costs.slippage_bps = 0
    s.costs.funding_bps_per_8h = 0
    return s


def test_backtest_stops_and_reverses_on_every_cross():
    candles = [bar(i, c) for i, c in enumerate(zigzag(8))]
    r = run_backtest(candles, settings(), "ETHUSDT", starting_equity=1000)
    sides = [t.side for t in r.trades]
    assert len(r.trades) >= 6
    assert all(a != b for a, b in zip(sides, sides[1:]))  # LONG, SHORT, LONG, ...
    assert all(t.exit_reason == "REVERSE" for t in r.trades[:-1]) and r.trades[-1].exit_reason == "END"
    for prev, nxt in zip(r.trades, r.trades[1:]):
        assert nxt.entry_time == prev.exit_time  # the new side fills at the same open
    assert sum(t.net_pnl for t in r.trades) > 0  # clean swings: each leg's middle is caught


def test_backtest_without_reversal_holds_until_stop_or_target():
    candles = [bar(i, c) for i, c in enumerate(zigzag(8))]
    s = settings()
    s.management.exit_on_opposite_signal = False
    r = run_backtest(candles, s, "ETHUSDT", starting_equity=1000)
    assert r.trades and all(t.exit_reason != "REVERSE" for t in r.trades)


def test_backtest_reversal_passes_the_risk_gate_after_the_exit_is_booked():
    # like live: the closed trade is booked first, so its cooldown applies to the new side
    candles = [bar(i, c) for i, c in enumerate(zigzag(8))]
    s = settings()
    s.management.cooldown_bars_after_exit = 1
    r = run_backtest(candles, s, "ETHUSDT", starting_equity=1000)
    assert len({t.side for t in r.trades}) == 1  # every reversal blocked: only the first side trades
    assert r.skipped["symbol cooldown"] >= 2
    assert any("cooldown" in w for w in validate(s))


def test_backtest_trend_filter_exits_without_reversing():
    candles = flat(30)
    candles += [ohlc(30 + i, 100, 100.2, 99.8, 100) for i in range(10)]
    plan = {candles[29].open_time: (LONG, 98.0, 3.0)}
    exit_at = candles[34].open_time

    class Exits(ScriptedStrategy):
        def should_exit(self, side):
            return self.last_candle is not None and self.last_candle.open_time == exit_at

    s = Settings()
    s.symbols = ["BTCUSDT"]
    s.risk.min_sl_cost_ratio = 0
    s.management.exit_on_opposite_signal = True
    s.management.breakeven_at_r = 0
    r = run_backtest(candles, s, "BTCUSDT", strategy=Exits("BTCUSDT", plan=plan), starting_equity=1000)
    [t] = r.trades
    assert t.exit_reason == "REVERSE" and t.exit_time == candles[35].open_time


# -- config -----------------------------------------------------------------
def test_ema_config_is_eth_3m_stop_and_reverse():
    s = load_settings("config/ema.yaml", env_path="/nonexistent.env", mode_override="paper")
    assert s.symbols == ["ETHUSDT"] and s.timeframe == "3m" and s.strategy.name == "ema_cross"
    p = s.strategy.ema_cross
    assert (p.fast, p.slow, p.sl_atr, p.min_sl_pct, p.tp_r, p.trend_ema) == (9, 21, 1.5, 0.5, 2.0, 0)
    assert s.management.exit_on_opposite_signal and s.management.cooldown_bars_after_exit == 0
    assert s.data_dir == "data/ema" and s.log_dir == "logs/ema"
    assert isinstance(build_strategy(s, "ETHUSDT"), EmaCrossStrategy)
    assert not any("cooldown" in w for w in validate(s))


def test_minimum_stop_passes_the_fee_filter():
    s = load_settings("config/ema.yaml", env_path="/nonexistent.env", mode_override="paper")
    strat = build_strategy(s, "ETHUSDT")
    closes = [2000 - 0.5 * i for i in range(60)] + [1970 + 0.5 * i for i in range(1, 40)]
    sig = next(sig for i, c in enumerate(closes) if (sig := strat.on_candle(bar(i, c, spread=0.3))) is not None)
    assert sig.side == LONG
    assert (sig.entry_ref - sig.stop) / sig.entry_ref == pytest.approx(0.005)  # tiny ATR: the 0.5% floor
    assert fee_filter(sig, s.costs, s.risk) == ""
    sig.stop = sig.entry_ref * (1 - 0.003)  # a 0.3% stop would be mostly fees
    assert classify(fee_filter(sig, s.costs, s.risk)) == "fee_filter"


@pytest.mark.parametrize("bad", [dict(fast=21, slow=9), dict(fast=0), dict(tp_r=0), dict(sl_atr=0), dict(trend_ema=-1)])
def test_invalid_settings_are_refused(bad):
    s = settings(**bad)
    with pytest.raises(ValueError, match="ema_cross"):
        validate(s)


def test_optimizer_grid_is_valid():
    s = settings()
    combo = {k: v[0] for k, v in GRIDS["ema_cross"].items()}
    _apply_combo(s, combo)
    assert s.strategy.ema_cross.min_sl_pct == combo["min_sl_pct"]
    assert s.management.exit_on_opposite_signal is combo["mgmt_exit_on_opposite_signal"]
    validate(s)
