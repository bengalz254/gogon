from decimal import Decimal

import pytest

from scalper.config import CostConfig, ManagementConfig, RiskConfig
from scalper.models import LONG, SHORT, Candle, OrderRef, Signal, SymbolRules
from scalper.risk import RiskGuard, evaluate_entry, fee_filter, size_position
from scalper.trade import Trade, manage_trade, simulate_exit, targets_from_fill

H = 3_600_000
DAY = 86_400_000


def c(o, h, l, cl, t=0, step=300_000):
    return Candle(t, o, h, l, cl, 1.0, t + step - 1)


def long_trade(**kw):
    base = dict(trade_id="t", symbol="BTCUSDT", side=LONG, strategy="x", qty=1.0, entry_price=100.0,
                stop=99.0, initial_stop=99.0, take_profit=101.5, opened_at=0)
    base.update(kw)
    return Trade(**base)


def short_trade(**kw):
    base = dict(trade_id="t", symbol="BTCUSDT", side=SHORT, strategy="x", qty=1.0, entry_price=100.0,
                stop=101.0, initial_stop=101.0, take_profit=98.5, opened_at=0)
    base.update(kw)
    return Trade(**base)


# -- simulate_exit -----------------------------------------------------------
def test_stop_wins_when_bar_touches_both():
    ex = simulate_exit(long_trade(), c(100, 102, 98.5, 101), slippage=0.001, tp_is_limit=True)
    assert ex.reason == "SL" and ex.price == pytest.approx(99.0 * 0.999) and not ex.maker


def test_gap_through_stop_fills_at_open():
    ex = simulate_exit(long_trade(), c(98.0, 98.5, 97.5, 98.2), slippage=0.0, tp_is_limit=True)
    assert ex.price == 98.0


def test_limit_target_needs_trade_through():
    t = long_trade()
    assert simulate_exit(t, c(100, 101.5, 99.5, 101), 0.0, True) is None  # touched only
    ex = simulate_exit(t, c(100, 101.6, 99.5, 101), 0.0, True)
    assert ex.reason == "TP" and ex.price == 101.5 and ex.maker


def test_market_target_fills_on_touch_with_slippage():
    ex = simulate_exit(long_trade(), c(100, 101.5, 99.5, 101), 0.001, False)
    assert ex.reason == "TP" and ex.price == pytest.approx(101.5 * 0.999) and not ex.maker


def test_short_exits_mirror_longs():
    t = short_trade()
    assert simulate_exit(t, c(100, 101.2, 98.0, 99), 0.0, True).reason == "SL"
    ex = simulate_exit(t, c(100, 100.5, 98.4, 99), 0.0, True)
    assert ex.reason == "TP" and ex.price == 98.5


def test_stop_kind_is_reported():
    t = long_trade(stop=100.2, stop_kind="BE")
    assert simulate_exit(t, c(100.5, 100.6, 100.0, 100.1), 0.0, True).reason == "BE"


# -- targets_from_fill ----------------------------------------------------------
def test_r_multiple_target_is_reanchored_to_fill():
    sig = Signal("BTCUSDT", LONG, "x", 100.0, 99.0, 101.5, 0.5, 0, "", tp_r=1.5)
    assert targets_from_fill(sig, 100.2) == (99.0, pytest.approx(100.2 + 1.5 * 1.2))
    assert targets_from_fill(sig, 98.9) is None  # filled below the stop


def test_fixed_target_kept_and_invalid_when_passed():
    sig = Signal("BTCUSDT", SHORT, "x", 100.0, 101.0, 99.0, 0.5, 0, "", tp_r=None)
    assert targets_from_fill(sig, 99.8) == (101.0, 99.0)
    assert targets_from_fill(sig, 98.9) is None


# -- manage_trade ----------------------------------------------------------------
def test_breakeven_after_one_r_includes_costs():
    t = long_trade()
    t.observe(c(100, 101.1, 100.0, 100.9, t=0))
    act = manage_trade(t, c(100, 101.1, 100.0, 100.9), atr=0.5, cfg=ManagementConfig(), round_trip_cost=0.0014)
    assert act.new_stop == pytest.approx(100.0 * 1.0014) and act.new_stop_kind == "BE"


def test_breakeven_skipped_if_close_is_too_near():
    t = long_trade()
    candle = c(100, 101.2, 99.9, 100.1)  # spiked to +1.2R but closed back near entry
    t.observe(candle)
    assert manage_trade(t, candle, 0.5, ManagementConfig(), 0.0014).new_stop is None


def test_trailing_stop_moves_only_forward():
    cfg = ManagementConfig(breakeven_at_r=0, trail_start_r=1.0, trail_atr=1.0)
    t = long_trade()
    first = c(100, 102.0, 100.5, 101.8, t=0)
    t.observe(first)
    act = manage_trade(t, first, 0.5, cfg, 0.0)
    assert act.new_stop == pytest.approx(101.5) and act.new_stop_kind == "TRAIL"
    t.stop = act.new_stop
    second = c(101.8, 101.9, 101.6, 101.7, t=300_000)
    t.observe(second)
    assert manage_trade(t, second, 0.5, cfg, 0.0).new_stop is None  # best price unchanged


def test_short_breakeven():
    t = short_trade()
    candle = c(100, 100.2, 98.9, 99.1)
    t.observe(candle)
    act = manage_trade(t, candle, 0.5, ManagementConfig(), 0.001)
    assert act.new_stop == pytest.approx(99.9)


def test_time_stop():
    t = long_trade(bars_held=24)
    assert manage_trade(t, c(100, 100, 100, 100), 0.5, ManagementConfig(), 0.0).exit_reason == "TIME"


def test_observe_counts_each_candle_once_and_only_after_entry():
    t = long_trade(opened_at=1_000_000)
    assert not t.observe(c(1, 1, 1, 1, t=600_000))  # ended before entry
    assert t.observe(c(1, 1, 1, 1, t=900_000))
    assert not t.observe(c(1, 1, 1, 1, t=900_000))
    assert t.bars_held == 1


def test_trade_roundtrips_through_dict():
    t = long_trade(sl_order=OrderRef("algo", "1", "scsl1", "sl", "STOP_MARKET", 99.0))
    again = Trade.from_dict(t.to_dict())
    assert again == t


# -- sizing ----------------------------------------------------------------------------
def rules():
    return SymbolRules("BTCUSDT", Decimal("0.1"), Decimal("0.001"), Decimal("0.001"), Decimal("1000"),
                       Decimal("0.001"), Decimal("0.001"), Decimal("120"), Decimal("100"))


def test_size_risks_the_configured_fraction_including_costs():
    costs = CostConfig(taker_fee=0.0005, slippage_bps=2)
    res = size_position(10_000, 10_000, 50_000, 49_500, rules(), RiskConfig(risk_per_trade_pct=1.0), costs, 10)
    assert res.ok
    assert res.risk_usd <= 100.0 + 1e-6
    assert res.risk_usd == pytest.approx(100.0, rel=0.01)
    # 100 / (500 stop + 69.65 costs per BTC) = 0.17555 -> rounded DOWN to the 0.001 step
    assert res.qty == pytest.approx(0.175)


def test_size_capped_by_margin():
    res = size_position(1_000, 1_000, 100.0, 99.99, None, RiskConfig(risk_per_trade_pct=1.0, max_margin_use_pct=50),
                        CostConfig(taker_fee=0, slippage_bps=0), 5)
    assert res.notional == pytest.approx(2_500)


def test_size_rejects_below_min_notional():
    res = size_position(100, 100, 50_000, 49_000, rules(), RiskConfig(risk_per_trade_pct=0.5), CostConfig(), 10)
    assert not res.ok and "minimum" in res.reason


def test_size_rejects_stop_near_liquidation():
    res = size_position(1_000, 1_000, 100.0, 95.0, None, RiskConfig(), CostConfig(), 20)
    assert not res.ok and "liquidation" in res.reason


def test_fee_filter():
    costs = CostConfig(taker_fee=0.0005, slippage_bps=2)  # round trip 0.14%
    tight = Signal("X", LONG, "s", 100.0, 99.8, 100.3, 0.1, 0, "")  # 0.2% stop
    wide = Signal("X", LONG, "s", 100.0, 99.5, 100.75, 0.1, 0, "")  # 0.5% stop
    assert "fees" in fee_filter(tight, costs, RiskConfig(min_sl_cost_ratio=3))
    assert fee_filter(wide, costs, RiskConfig(min_sl_cost_ratio=3)) == ""


# -- risk guard -----------------------------------------------------------------------
def test_daily_loss_limit_and_reset_next_day():
    g = RiskGuard(RiskConfig(max_daily_loss_pct=2.0))
    g.start(1_000, 0)
    g.on_trade_closed("X", -20.0, 1 * H)
    ok, reason = g.can_open("X", 2 * H, 0)
    assert not ok and "daily loss" in reason
    ok, _ = g.can_open("X", DAY + H, 0)
    assert ok


def test_loss_streak_cooldown():
    g = RiskGuard(RiskConfig(max_consecutive_losses=3, loss_cooldown_minutes=60, max_daily_loss_pct=50))
    g.start(1_000, 0)
    for i in range(3):
        g.on_trade_closed("X", -1.0, i * 1000)
    ok, reason = g.can_open("X", 10_000, 0)
    assert not ok and "cooling down" in reason
    assert g.can_open("X", 2_000 + 60 * 60_000 + 1, 0)[0]


def test_drawdown_halts_until_reset():
    g = RiskGuard(RiskConfig(max_drawdown_pct=10, max_daily_loss_pct=100, max_consecutive_losses=0))
    g.start(1_000, 0)
    g.on_trade_closed("X", 200.0, 1)  # peak 1200
    g.on_trade_closed("X", -130.0, 2)  # 1070 -> 10.8% below peak
    ok, reason = g.can_open("X", 3, 0)
    assert not ok and "halted" in reason and g.s.halted
    assert not g.can_open("X", DAY * 5, 0)[0]  # a new day does not clear a halt
    g.reset_halt()
    assert g.can_open("X", DAY * 5, 0)[0]


def test_symbol_cooldown_and_position_limits():
    g = RiskGuard(RiskConfig(max_open_positions=1, max_trades_per_day=2))
    g.start(1_000, 0)
    g.on_trade_closed("X", 1.0, 0, cooldown_ms=600_000)
    assert "cooldown" in g.can_open("X", 300_000, 0)[1]
    assert g.can_open("Y", 300_000, 0)[0]
    assert "max_open_positions" in g.can_open("Y", 300_000, 1)[1]
    g.on_trade_opened("Y", 1)
    g.on_trade_opened("Y", 2)
    assert "max_trades_per_day" in g.can_open("Y", 3, 0)[1]


def test_risk_guard_persists():
    g = RiskGuard(RiskConfig())
    g.start(1_000, 0)
    g.on_trade_closed("X", -5.0, 1)
    again = RiskGuard.from_dict(RiskConfig(), g.to_dict())
    assert again.s.realized_today == -5.0 and again.value == 995.0


def test_evaluate_entry_rejects_price_past_stop():
    g = RiskGuard(RiskConfig())
    g.start(1_000, 0)
    sig = Signal("X", LONG, "s", 100.0, 99.0, 101.5, 0.5, 0, "")
    d = evaluate_entry(sig, 98.9, 1_000, 1_000, 0, 1, g, None, RiskConfig(), CostConfig(), 5)
    assert not d.ok and "stop" in d.reason
    d = evaluate_entry(sig, 100.0, 1_000, 1_000, 0, 1, g, None, RiskConfig(), CostConfig(), 5)
    assert d.ok and d.size.qty > 0
