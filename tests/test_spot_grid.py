"""Grid math, paper fills and order placement for the Tokocrypto grid bot."""
import pytest

from spot.config import CostConfig, GridConfig
from spot.grid import (
    BUYING,
    HOLDING,
    IDLE,
    SELLING,
    Grid,
    build_levels,
    ceil_to,
    floor_to,
    fmt_amount,
    fmt_money,
    make_slots,
    plan_grid,
    slot_net_profit_pct,
)
from spot.market import Candle, MarketRules

RULES = MarketRules("BTC/IDR", "BTC", "IDR", tick=0.01, step=0.001, min_amount=0.001, min_cost=1.0, native=True)
COSTS = CostConfig()  # 0.13% per buy, 0.34% per sell


def candle(low, high, ts=0):
    return Candle(ts, (low + high) / 2, high, low, (low + high) / 2)


def test_rounding_helpers_avoid_float_noise():
    assert floor_to(0.3, 0.1) == 0.3  # 0.3 / 0.1 is 2.9999999999999996 in floats
    assert ceil_to(1.0000001, 0.01) == 1.01
    assert floor_to(0.00006944, 0.00001) == 0.00006


def test_levels_are_geometric_and_on_the_tick():
    levels = build_levels(90, 110, 5, 0.01)
    assert levels[0] == 90 and levels[-1] == 110
    gaps = [b / a for a, b in zip(levels, levels[1:])]
    assert max(gaps) - min(gaps) < 1e-3  # same percentage per gap
    assert all(round(level, 2) == level for level in levels)


def test_costs_split_into_fee_and_tax():
    assert COSTS.cost_rate("BUY") == pytest.approx(0.0013)
    assert COSTS.cost_rate("SELL") == pytest.approx(0.0034)
    assert COSTS.cost_rate("SELL", taker=True) == pytest.approx(0.0044)


def test_plan_reports_profit_after_costs():
    plan = plan_grid(90, 110, GridConfig(levels=5, order_value=100), COSTS, RULES, balance=1000)
    assert plan.problems == []
    assert len(plan.slots) == 4
    assert plan.min_gap_pct == pytest.approx(5.14, abs=0.02)
    assert plan.round_trip_cost_pct == pytest.approx(0.47)
    assert plan.net_profit_pct == pytest.approx(plan.min_gap_pct - 0.47, abs=0.03)


def test_plan_refuses_gaps_that_costs_eat():
    plan = plan_grid(99, 101, GridConfig(levels=10, order_value=100), COSTS, RULES, balance=10_000)
    assert any("terlalu rapat" in p for p in plan.problems)


def test_plan_refuses_orders_below_the_minimum():
    rules = MarketRules("BTC/IDR", "BTC", "IDR", tick=0.01, step=0.001, min_amount=0.001, min_cost=50.0, native=True)
    plan = plan_grid(90, 110, GridConfig(levels=5, order_value=40), COSTS, rules, balance=1000)
    assert any("order_value terlalu kecil" in p for p in plan.problems)


def test_plan_refuses_a_grid_the_balance_cannot_fund():
    plan = plan_grid(90, 110, GridConfig(levels=5, order_value=300), COSTS, RULES, balance=1000)
    assert any("Modal kurang" in p for p in plan.problems)


def test_plan_refuses_levels_that_collapse_on_the_tick():
    rules = MarketRules("X/IDR", "X", "IDR", tick=1.0, step=1.0, min_amount=0, min_cost=0, native=True)
    plan = plan_grid(10, 12, GridConfig(levels=10, order_value=100), COSTS, rules, balance=10_000)
    assert any("terlalu sempit" in p for p in plan.problems)


def make_grid(balance=1000.0):
    plan = plan_grid(90, 110, GridConfig(levels=5, order_value=100), COSTS, RULES, balance)
    return Grid(plan.slots, balance, 0.0, COSTS)


def test_starts_with_buys_below_the_price_only():
    grid = make_grid()
    buys, sells = grid.place_orders(bid=99.9, ask=100.1, tick=0.01)
    assert (buys, sells) == (3, 0)
    assert [s.state for s in grid.slots] == [BUYING, BUYING, BUYING, IDLE]
    assert grid.available_quote == pytest.approx(1000 - grid.reserved_quote)


def test_buy_then_sell_books_one_gap_minus_costs():
    grid = make_grid()
    grid.place_orders(99.9, 100.1, 0.01)
    slot = grid.slots[2]  # buys at ~99.5, sells at ~104.6

    # Touching the level isn't a fill; trading through it is.
    assert grid.apply_candle(candle(slot.buy_price, 101)) == []
    [buy] = grid.apply_candle(candle(slot.buy_price - 0.01, 101))
    assert buy.side == "BUY" and slot.state == HOLDING
    assert grid.base == pytest.approx(slot.amount)

    # A sell rests one level up only after the next placement pass.
    assert grid.apply_candle(candle(99, 120)) == []
    grid.place_orders(99.0, 99.2, 0.01)
    assert slot.state == SELLING and slot.order_price == slot.sell_price
    [sell] = grid.apply_candle(candle(99, slot.sell_price + 0.01))
    assert sell.side == "SELL" and slot.state == IDLE
    expected = slot.amount * (slot.sell_price * (1 - 0.0034) - slot.buy_price * (1 + 0.0013))
    assert sell.pnl == pytest.approx(expected)
    assert grid.base == pytest.approx(0)
    assert grid.quote == pytest.approx(1000 + expected)


def test_sell_goes_above_the_bid_when_price_already_ran_past_its_level():
    grid = make_grid()
    grid.place_orders(99.9, 100.1, 0.01)
    slot = grid.slots[2]
    grid.apply_candle(candle(slot.buy_price - 1, 101))
    grid.place_orders(bid=106.0, ask=106.2, tick=0.01)
    assert slot.order_price == 106.01  # maker, and better than the planned level


def test_buy_waits_while_its_level_is_not_below_the_ask():
    grid = make_grid()
    grid.place_orders(bid=94.0, ask=94.2, tick=0.01)
    assert [s.state for s in grid.slots] == [BUYING, IDLE, IDLE, IDLE]
    grid.place_orders(bid=108.0, ask=108.2, tick=0.01)  # the price rose past them
    assert [s.state for s in grid.slots] == [BUYING, BUYING, BUYING, BUYING]


def test_limited_money_funds_the_levels_nearest_the_price():
    grid = make_grid(balance=210.0)  # enough for two orders of ~100
    grid.place_orders(99.9, 100.1, 0.01)
    assert [s.state for s in grid.slots] == [IDLE, BUYING, BUYING, IDLE]


def test_liquidate_sells_everything_as_taker_and_drops_buys():
    grid = make_grid()
    grid.place_orders(99.9, 100.1, 0.01)
    grid.apply_candle(candle(80, 100))  # all three buys fill
    grid.place_orders(80.0, 80.2, 0.01)
    fills = grid.liquidate(bid=80.0)
    assert len(fills) == 3 and all(f.taker and f.side == "SELL" for f in fills)
    assert all(s.state == IDLE for s in grid.slots)
    assert grid.base == pytest.approx(0)
    assert sum(f.pnl for f in fills) < 0
    assert all(f.fee == pytest.approx(f.value * 0.0023) for f in fills)  # taker 0.20% + levy 0.03%


def test_equity_is_money_plus_coins_at_the_bid_after_selling_costs():
    grid = make_grid()
    grid.place_orders(99.9, 100.1, 0.01)
    grid.apply_candle(candle(95, 100))  # the 99.5 buy fills
    slot = grid.slots[2]
    assert grid.equity(96.0) == pytest.approx(grid.quote + slot.amount * 96.0 * (1 - 0.0034))
    assert grid.unrealized(96.0) == pytest.approx(slot.amount * 96.0 * (1 - 0.0034) - slot.cost)


def test_round_trip_through_dict():
    grid = make_grid()
    grid.place_orders(99.9, 100.1, 0.01)
    grid.apply_candle(candle(95, 100))
    copy = Grid.from_dict(grid.to_dict(), COSTS)
    assert copy.to_dict() == grid.to_dict()


def test_net_profit_uses_both_sides_costs():
    [slot] = make_slots([100.0, 101.0], 100, 0.001)
    assert slot_net_profit_pct(slot, COSTS) == pytest.approx((101 * 0.9966 / (100 * 1.0013) - 1) * 100)


def test_formatting_in_indonesian_style():
    assert fmt_money(1587000, "IDR") == "Rp 1.587.000"
    assert fmt_money(-1234.4, "IDR") == "-Rp 1.234"
    assert fmt_money(12.5, "USDT") == "12.50 USDT"
    assert fmt_amount(0.00006) == "0.00006"
    assert fmt_amount(2.0) == "2"
