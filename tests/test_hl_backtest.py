import pytest

from hlbot.backtest import run_backtest
from hlbot.indicators import ema
from hlbot.strategy import LONG, SHORT, Candle, cross_at

from hl_helpers import candles_from_closes, down_then_up, frictionless, strategy_cfg, trade_cfg


def signal_indices(candles):
    closes = [c.c for c in candles]
    f, s = ema(closes, 9), ema(closes, 21)
    return [(i, cross_at(f, s, i)) for i in range(len(closes)) if cross_at(f, s, i)]


def test_long_then_trailing_exit_then_short():
    closes = down_then_up() + [100.0 - 0.5 * i for i in range(1, 41)]
    candles = candles_from_closes(closes)
    sigs = signal_indices(candles)
    assert [s for _, s in sigs] == [LONG, SHORT]

    res = run_backtest(candles, strategy_cfg(), trade_cfg(), frictionless())
    assert [t.side for t in res.trades] == [LONG, SHORT]

    long_trade = res.trades[0]
    # entered at the OPEN of the candle after the (closed) cross candle
    assert long_trade.entry_price == pytest.approx(candles[sigs[0][0] + 1].o)
    assert long_trade.exit_reason == "TRAILING_STOP"
    assert long_trade.pnl_usd > 0
    # exit at 0.5% below the peak price
    assert long_trade.exit_price == pytest.approx(max(closes) * 0.995)

    short_trade = res.trades[1]
    assert short_trade.entry_price == pytest.approx(candles[sigs[1][0] + 1].o)
    assert short_trade.exit_reason == "END_OF_DATA"


def test_reverse_signal_closes_opposite_then_opens_new_side():
    closes = down_then_up() + [100.0 - 0.5 * i for i in range(1, 41)]
    candles = candles_from_closes(closes)
    # TP never reachable -> only the opposite cross can close the long
    res = run_backtest(candles, strategy_cfg(), trade_cfg(take_profit_pct=0.9), frictionless())
    assert [(t.side, t.exit_reason) for t in res.trades] == [(LONG, "REVERSE_SIGNAL"), (SHORT, "END_OF_DATA")]
    assert res.trades[0].exit_time == res.trades[1].entry_time


def test_idle_without_cross():
    closes = [100.0 + 0.1 * i for i in range(120)]  # steady uptrend from the start: no cross ever
    res = run_backtest(candles_from_closes(closes), strategy_cfg(), trade_cfg(), frictionless())
    assert res.trades == []
    assert res.net_pnl_usd == 0


def test_liquidation_loses_whole_margin():
    closes = down_then_up(n_up=16)
    candles = candles_from_closes(closes)
    entry_idx = signal_indices(candles)[0][0] + 1
    assert entry_idx < len(candles) - 1
    last = candles[-1]
    crash = Candle(t=last.t + 1_800_000, o=last.c, h=last.c, l=last.c * 0.85, c=last.c * 0.86)
    res = run_backtest(candles + [crash], strategy_cfg(), trade_cfg(take_profit_pct=0.5), frictionless())
    assert res.trades[0].exit_reason == "LIQUIDATION"
    assert res.trades[0].pnl_usd == pytest.approx(-100.0)
    assert res.summary()["liquidations"] == 1


def test_fees_and_slippage_reduce_pnl():
    closes = down_then_up() + [100.0 - 0.5 * i for i in range(1, 41)]
    candles = candles_from_closes(closes)
    clean = run_backtest(candles, strategy_cfg(), trade_cfg(), frictionless())
    costly_cfg = frictionless()
    costly_cfg.taker_fee = 0.00045
    costly_cfg.slippage = 0.0005
    costly = run_backtest(candles, strategy_cfg(), trade_cfg(), costly_cfg)
    assert costly.net_pnl_usd < clean.net_pnl_usd
    assert costly.summary()["fees_usd"] > 0


def test_pnl_scales_with_leverage():
    closes = down_then_up() + [100.0 - 0.5 * i for i in range(1, 41)]
    candles = candles_from_closes(closes)
    r10 = run_backtest(candles, strategy_cfg(), trade_cfg(leverage=10), frictionless())
    r5 = run_backtest(candles, strategy_cfg(), trade_cfg(leverage=5), frictionless())
    assert r10.trades[0].roe_pct == pytest.approx(2 * r5.trades[0].roe_pct)


def test_summary_and_drawdown_fields():
    closes = down_then_up() + [100.0 - 0.5 * i for i in range(1, 41)]
    res = run_backtest(candles_from_closes(closes, wick=0.001), strategy_cfg(), trade_cfg(), frictionless())
    s = res.summary()
    assert s["trades"] == len(res.trades)
    assert s["max_drawdown_usd"] >= 0
    assert s["final_equity_usd"] == pytest.approx(1000.0 + res.net_pnl_usd, abs=0.01)


def test_requires_enough_candles():
    with pytest.raises(ValueError):
        run_backtest(candles_from_closes([100.0] * 10), strategy_cfg(), trade_cfg(), frictionless())


def test_margin_basis_divides_by_leverage():
    t = trade_cfg(pct_basis="margin", leverage=10, stop_loss_pct=0.3)
    assert t.tp_price_pct == pytest.approx(0.002)
    assert t.trailing_price_pct == pytest.approx(0.0005)
    assert t.stop_loss_price_pct == pytest.approx(0.03)
    p = trade_cfg(pct_basis="price")
    assert p.tp_price_pct == 0.02 and p.trailing_price_pct == 0.005


def test_margin_basis_backtest_roe_matches_config():
    closes = down_then_up() + [100.0 - 0.5 * i for i in range(1, 41)]
    candles = candles_from_closes(closes)
    res = run_backtest(candles, strategy_cfg(), trade_cfg(pct_basis="margin"), frictionless("conservative"))
    first = res.trades[0]
    assert first.side == LONG and first.exit_reason == "TRAILING_STOP"
    # TP 2% of margin, trailing 0.5% of margin -> at least ~1.5% ROE locked in
    assert first.roe_pct == pytest.approx(1.5, abs=0.01)
    assert first.exit_price == pytest.approx(first.entry_price * 1.002 * (1 - 0.0005))


def test_conservative_intrabar_takes_minimum_locked_profit():
    closes = down_then_up() + [100.0 - 0.5 * i for i in range(1, 41)]
    candles = candles_from_closes(closes)
    ohlc = run_backtest(candles, strategy_cfg(), trade_cfg(), frictionless("ohlc"))
    cons = run_backtest(candles, strategy_cfg(), trade_cfg(), frictionless("conservative"))
    entry = cons.trades[0].entry_price
    assert cons.trades[0].exit_price == pytest.approx(entry * 1.02 * 0.995)
    assert cons.trades[0].pnl_usd < ohlc.trades[0].pnl_usd  # ohlc rides the trend to the peak


def test_conservative_does_not_change_reverse_or_liquidation_exits():
    closes = down_then_up() + [100.0 - 0.5 * i for i in range(1, 41)]
    candles = candles_from_closes(closes)
    cfg = trade_cfg(take_profit_pct=0.9, trailing_pct=0.1)
    a = run_backtest(candles, strategy_cfg(), cfg, frictionless("ohlc"))
    b = run_backtest(candles, strategy_cfg(), cfg, frictionless("conservative"))
    assert [(t.exit_reason, t.pnl_usd) for t in a.trades] == [(t.exit_reason, t.pnl_usd) for t in b.trades]


def test_sweep_covers_grid_and_sorts_by_conservative_pnl():
    from hlbot.backtest import sweep

    closes = down_then_up() + [100.0 - 0.5 * i for i in range(1, 41)]
    candles = candles_from_closes(closes, wick=0.002)
    rows = sweep(candles, strategy_cfg(), trade_cfg(), frictionless(), tps=(0.05, 0.2), trails=(0.005, 0.1),
                 stop_losses=(None, 0.3))
    # (0.05, 0.1) is skipped because trailing must be below TP
    assert len(rows) == 3 * 2
    pnls = [r["net_pnl_conservative"] for r in rows]
    assert pnls == sorted(pnls, reverse=True)
    assert all(r["net_pnl_ohlc"] >= r["net_pnl_conservative"] - 1e-9 for r in rows)
