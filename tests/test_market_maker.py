from datetime import datetime, timedelta, timezone

from bot.config import MarketMakerConfig
from bot.market_data import MarketInfo, RewardInfo, TokenInfo
from bot.orderbook import OrderBook
from bot.strategies.market_maker import (
    MidTracker,
    Quote,
    ceil_to_tick,
    compute_quotes,
    floor_to_tick,
    select_markets,
    skip_reason,
)

NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def market(condition_id="mkt1", rewards=RewardInfo(min_size=10, max_spread=0.03, daily_rate=50), **kw):
    return MarketInfo(
        condition_id=condition_id,
        question=f"Will {condition_id} happen?",
        tokens=[TokenInfo(f"{condition_id}-yes", "Yes"), TokenInfo(f"{condition_id}-no", "No")],
        rewards=rewards,
        tick_size=0.01,
        **kw,
    )


def book(bid, ask, token="mkt1-yes", tick=0.01):
    return OrderBook(token, bids={bid: 100.0}, asks={ask: 100.0}, tick_size=tick)


def cfg(**overrides):
    base = dict(enabled=True, order_size=10, spread_fraction=0.6, max_inventory=100, inventory_skew=0.5)
    base.update(overrides)
    return MarketMakerConfig(**base)


def test_tick_rounding():
    assert floor_to_tick(0.482, 0.01) == 0.48 and ceil_to_tick(0.482, 0.01) == 0.49
    assert floor_to_tick(0.49, 0.01) == 0.49 and ceil_to_tick(0.49, 0.01) == 0.49  # float noise ignored
    assert floor_to_tick(0.4837, 0.001) == 0.483


def test_flat_inventory_buys_both_sides_around_the_mid():
    quotes = compute_quotes(market(), book(0.48, 0.52), {}, cfg())
    # half-spread = 0.6 * 3c = 1.8c around mid 0.50 -> bid 0.48, ask 0.52 (NO bought at 0.48)
    assert quotes == [Quote("mkt1-yes", "BUY", 0.48, 10), Quote("mkt1-no", "BUY", 0.48, 10)]


def test_inventory_is_sold_instead_of_buying_the_complement_and_quotes_lean():
    quotes = compute_quotes(market(), book(0.48, 0.52), {"mkt1-yes": 30.0}, cfg())
    # long 30 YES: lean 0.5 * 1.8c * 0.3 lowers both quotes; the ask side sells YES
    assert quotes == [Quote("mkt1-yes", "BUY", 0.47, 10), Quote("mkt1-yes", "SELL", 0.52, 10)]
    quotes = compute_quotes(market(), book(0.48, 0.52), {"mkt1-no": 30.0}, cfg())
    assert quotes[0] == Quote("mkt1-no", "SELL", 0.52, 10)  # bid side sells the NO held (1 - 0.48)


def test_side_stops_at_the_inventory_limit():
    quotes = compute_quotes(market(), book(0.48, 0.52), {"mkt1-yes": 100.0}, cfg())
    assert [(q.token_id, q.side) for q in quotes] == [("mkt1-yes", "SELL")]
    quotes = compute_quotes(market(), book(0.48, 0.52), {"mkt1-no": 100.0}, cfg())
    assert [(q.token_id, q.side) for q in quotes] == [("mkt1-no", "SELL")]


def test_quotes_never_cross_the_book():
    # an aggressive lean would put the bid above the ask; post-only clamps it
    quotes = compute_quotes(market(), book(0.49, 0.51), {"mkt1-no": 90.0}, cfg(inventory_skew=3.0))
    sell_no = next(q for q in quotes if q.token_id == "mkt1-no")  # the bid side, sold as NO
    assert 1 - sell_no.price <= 0.50  # equivalent YES bid stays below the 0.51 ask


def test_no_quotes_outside_the_mid_band_or_on_a_one_sided_book():
    assert compute_quotes(market(), book(0.05, 0.07), {}, cfg()) == []
    one_sided = OrderBook("mkt1-yes", bids={0.5: 10.0}, tick_size=0.01)
    assert compute_quotes(market(), one_sided, {}, cfg()) == []


def test_size_meets_the_reward_minimum():
    quotes = compute_quotes(market(rewards=RewardInfo(min_size=50, max_spread=0.03)), book(0.48, 0.52), {}, cfg())
    assert {q.size for q in quotes} == {50}


def test_without_max_spread_the_default_half_spread_is_used():
    quotes = compute_quotes(market(rewards=RewardInfo(min_size=10)), book(0.40, 0.60), {}, cfg(default_half_spread=0.05))
    assert quotes[0].price == 0.45


def test_skip_reasons():
    c = cfg(min_days_to_end=3, avoid_game_start_hours=6)
    assert skip_reason(market(), c, NOW) is None
    assert skip_reason(market(rewards=None), c, NOW) == "no liquidity rewards"
    assert skip_reason(market(end_date=NOW + timedelta(days=1)), c, NOW) == "ends too soon"
    assert skip_reason(market(game_start_time=NOW + timedelta(hours=2)), c, NOW) == "game starts soon or has started"
    assert skip_reason(market(game_start_time=NOW - timedelta(hours=1)), c, NOW) == "game starts soon or has started"
    assert skip_reason(market(seconds_delay=3), c, NOW) is not None
    assert skip_reason(market(accepting_orders=False), c, NOW) == "not accepting orders"


def test_select_ranks_by_reward_and_filters_by_mid_and_cost():
    markets = [
        market("low", rewards=RewardInfo(min_size=10, max_spread=0.03, daily_rate=5)),
        market("high", rewards=RewardInfo(min_size=10, max_spread=0.03, daily_rate=100)),
        market("extreme", rewards=RewardInfo(min_size=10, max_spread=0.03, daily_rate=500)),
        market("pricey", rewards=RewardInfo(min_size=1000, max_spread=0.03, daily_rate=400)),
        market("none", rewards=None),
    ]
    books = {
        "low-yes": book(0.48, 0.52, "low-yes"),
        "high-yes": book(0.48, 0.52, "high-yes"),
        "extreme-yes": book(0.02, 0.04, "extreme-yes"),
        "pricey-yes": book(0.48, 0.52, "pricey-yes"),
    }
    chosen = select_markets(markets, books.get, cfg(max_markets=5), NOW, max_quote_usd=25)
    # extreme mid is out of band; 1000 shares * 0.5 = $500 is over the $25 budget
    assert [m.condition_id for m in chosen] == ["high", "low"]
    assert [m.condition_id for m in select_markets(markets, books.get, cfg(max_markets=1), NOW, 25)] == ["high"]


def test_mid_tracker_measures_the_range_within_the_window():
    tracker = MidTracker(window_seconds=60)
    assert tracker.add("m", 0.50, now=0) == 0
    assert abs(tracker.add("m", 0.53, now=30) - 0.03) < 1e-12
    assert abs(tracker.add("m", 0.52, now=80) - 0.01) < 1e-12  # the 0.50 sample (age 80 s) aged out
    tracker.reset("m")
    assert tracker.add("m", 0.40, now=81) == 0
