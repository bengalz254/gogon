from datetime import datetime, timezone

from bot.config import MarketFilterConfig
from bot.market_data import _parse_market, iter_active_markets

RAW = {
    "condition_id": "0xabc",
    "question": "Will it happen?",
    "tokens": [{"token_id": "1", "outcome": "Yes"}, {"token_id": "2", "outcome": "No"}],
    "end_date_iso": "2026-12-31T00:00:00Z",
    "game_start_time": "2026-10-01 18:30:00",
    "accepting_orders": True,
    "seconds_delay": 3,
    "minimum_order_size": 5,
    "minimum_tick_size": 0.01,
    "neg_risk": True,
    "neg_risk_market_id": "0xevent",
    "rewards": {"rates": [{"asset_address": "0x", "rewards_daily_rate": 25}], "min_size": 50, "max_spread": 3.5},
}


def test_parses_market_terms_needed_for_trading():
    m = _parse_market(RAW)
    assert m.end_date == datetime(2026, 12, 31, tzinfo=timezone.utc)
    assert m.game_start_time == datetime(2026, 10, 1, 18, 30, tzinfo=timezone.utc)  # naive -> UTC
    assert (m.seconds_delay, m.min_order_size, m.tick_size) == (3.0, 5.0, 0.01)
    assert (m.neg_risk, m.neg_risk_market_id) == (True, "0xevent")
    assert (m.rewards.min_size, m.rewards.daily_rate) == (50.0, 25.0)
    assert abs(m.rewards.max_spread - 0.035) < 1e-12  # 3.5 cents


def test_missing_terms_default_safely():
    m = _parse_market({"condition_id": "0xabc", "tokens": [], "end_date_iso": "not a date"})
    assert m.end_date is None and m.game_start_time is None
    assert m.accepting_orders is True and m.seconds_delay == 0.0
    assert m.rewards is None and m.neg_risk is False


def test_max_spread_already_in_price_units_is_kept():
    m = _parse_market({**RAW, "rewards": {"min_size": 20, "max_spread": 0.03}})
    assert m.rewards.max_spread == 0.03 and m.rewards.daily_rate == 0.0


class PagedClient:
    def __init__(self, pages):
        self.pages = pages
        self.cursors = []

    def get_sampling_markets(self, next_cursor="MA=="):
        self.cursors.append(next_cursor)
        return self.pages[next_cursor]


def test_iteration_skips_markets_not_accepting_orders_and_stops_at_end_cursor():
    closed_book = {**RAW, "condition_id": "0xdef", "accepting_orders": False}
    client = PagedClient({"MA==": {"data": [RAW, closed_book], "next_cursor": "LTE="}})
    cfg = MarketFilterConfig(min_volume_usd=0, min_liquidity_usd=0)
    assert [m.condition_id for m in iter_active_markets(client, cfg)] == ["0xabc"]
    assert client.cursors == ["MA=="]
