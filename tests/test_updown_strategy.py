import pytest

from updown.config import FeeConfig, ModelConfig, SizingConfig, StrategyConfig
from updown.strategy import DOWN, UP, Book, Snapshot, UpDownStrategy, WindowPosition


def make_strategy(**strategy_overrides):
    return UpDownStrategy(
        StrategyConfig(**strategy_overrides),
        ModelConfig(vol_multiplier=1.0, basis_sd=0.0),
        FeeConfig(fee_rate=0.25, fee_exponent=2.0),
        SizingConfig(bankroll_usd=1000, kelly_fraction=0.25, max_bet_usd=50, max_window_exposure_usd=80, min_order_usd=1),
    )


def book(bid, ask, size=100.0):
    return Book.from_raw([(bid, size)] if bid else [], [(ask, size)] if ask else [])


def snap(spot=100.1, seconds_left=60, up=(0.60, 0.62), down=(0.38, 0.40)):
    # spot 0.1% above strike with 60s left at 1e-4 vol -> P(up) ~ 0.9
    return Snapshot(spot=spot, strike=100.0, seconds_left=seconds_left, sigma=1e-4, books={UP: book(*up), DOWN: book(*down)})


def test_buys_underpriced_side_with_positive_edge_after_fees():
    s = make_strategy()
    d, _ = s.decide(snap(), WindowPosition(), room_usd=100)
    assert d is not None and d.side == "BUY" and d.outcome == UP
    assert d.limit_price == 0.62
    per_share_cost = (d.usd + d.fee_usd) / d.shares
    assert d.fair_prob - per_share_cost >= 0.04
    assert d.usd <= 50  # max_bet_usd


def test_no_trade_when_market_is_fair():
    s = make_strategy()
    p = s.prob_up(snap())
    fair = snap(up=(round(p - 0.01, 2), round(p + 0.01, 2)), down=(round(1 - p - 0.01, 2), round(1 - p + 0.01, 2)))
    d, why = s.decide(fair, WindowPosition(), room_usd=100)
    assert d is None and "edge" in why


def test_only_trades_in_the_configured_slice_of_the_window():
    s = make_strategy()
    assert s.decide(snap(seconds_left=280), WindowPosition(), 100)[0] is None
    assert s.decide(snap(seconds_left=5), WindowPosition(), 100)[0] is None


def test_refuses_when_model_and_market_disagree_wildly():
    # Model says ~0.9 but the market has Up at 0.30: more likely our data is wrong.
    s = make_strategy()
    d, why = s.decide(snap(up=(0.28, 0.30), down=(0.70, 0.72)), WindowPosition(), 100)
    assert d is None and "distrusting" in why


def test_respects_price_band():
    s = make_strategy(max_price=0.60)
    assert s.decide(snap(), WindowPosition(), 100)[0] is None


def test_walks_the_book_only_while_edge_holds():
    s = make_strategy()
    deep = snap()
    deep.books[UP] = Book.from_raw([(0.60, 100)], [(0.62, 10), (0.70, 10), (0.95, 1000)])
    d, _ = s.decide(deep, WindowPosition(), 100)
    assert d.limit_price == 0.70  # 0.95 has no edge
    assert d.shares == pytest.approx(20)


def test_never_holds_both_sides_and_caps_entries():
    s = make_strategy(max_entries_per_window=1)
    pos = WindowPosition()
    pos.shares[DOWN] = 10
    pos.cost_usd[DOWN] = 4
    d, _ = s.decide(snap(), pos, 100)
    assert d is None or d.outcome != UP

    pos2 = WindowPosition(entries=1)
    assert s.decide(snap(), pos2, 100)[0] is None


def test_room_and_window_caps_limit_size():
    s = make_strategy()
    assert s.decide(snap(), WindowPosition(), room_usd=0.5)[0] is None
    d, _ = s.decide(snap(), WindowPosition(), room_usd=3)
    assert d.usd + d.fee_usd <= 3 + 1e-9


def test_takes_profit_when_bid_is_rich():
    s = make_strategy()
    pos = WindowPosition(entries=1)
    pos.shares[UP] = 20
    pos.cost_usd[UP] = 10
    # model ~0.9 for Up; someone bids 0.99 -> sell
    d, _ = s.decide(snap(up=(0.99, 0.995), down=(0.005, 0.01)), pos, 100)
    assert d.side == "SELL" and d.outcome == UP and d.shares == 20


def test_exit_allowed_even_with_no_risk_room():
    s = make_strategy()
    pos = WindowPosition(entries=1)
    pos.shares[UP] = 5
    d, _ = s.decide(snap(up=(0.99, 0.995), down=(0.005, 0.01)), pos, room_usd=0)
    assert d is not None and d.side == "SELL"


def test_vol_disagreement_alone_is_not_an_edge():
    """A market pricing with 25% higher vol than ours isn't wrong, just different."""
    s = UpDownStrategy(
        StrategyConfig(),
        ModelConfig(vol_multiplier=1.0, basis_sd=0.0, vol_uncertainty=0.3),
        FeeConfig(fee_rate=0.0),
        SizingConfig(bankroll_usd=1000, kelly_fraction=0.25, max_bet_usd=50),
    )
    base = Snapshot(spot=100.12, strike=100.0, seconds_left=120, sigma=1e-4, books={})
    market_p = s.prob_up(base, sigma_scale=1.25)
    ours = s.prob_up(base)
    assert ours - market_p > 0.04  # a naive model would see a 4c+ edge in Up
    base.books = {
        UP: Book.from_raw([(market_p - 0.005, 100)], [(market_p + 0.005, 100)]),
        DOWN: Book.from_raw([(1 - market_p - 0.005, 100)], [(1 - market_p + 0.005, 100)]),
    }
    assert s.decide(base, WindowPosition(), 100)[0] is None


def test_same_direction_room_across_coins_caps_or_blocks_entries():
    s = make_strategy()
    d, why = s.decide(snap(), WindowPosition(), room_usd=100, side_room={UP: 0.0, DOWN: 50.0})
    assert d is None and "same-direction" in why
    d, _ = s.decide(snap(), WindowPosition(), room_usd=100, side_room={UP: 3.0, DOWN: 50.0})
    assert d.outcome == UP and d.usd + d.fee_usd <= 3.0 + 1e-9


def test_waits_between_entries():
    s = make_strategy()
    pos = WindowPosition(entries=1)
    pos.shares[UP] = 5
    pos.cost_usd[UP] = 3
    d, why = s.decide(snap(), pos, 100, seconds_since_entry=5)
    assert d is None and "waiting" in why
    assert s.decide(snap(), pos, 100, seconds_since_entry=25)[0] is not None
