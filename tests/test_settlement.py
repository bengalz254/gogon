from bot.config import RiskConfig
from bot.risk import RiskManager
from bot.settlement import resolved_payouts, settle_resolved_markets


def binary_market(winner=None, closed=True):
    return {
        "condition_id": "mkt1",
        "closed": closed,
        "tokens": [
            {"token_id": "yes", "outcome": "Yes", "winner": winner == "yes"},
            {"token_id": "no", "outcome": "No", "winner": winner == "no"},
        ],
    }


class FakeClient:
    def __init__(self, markets):
        self.markets = markets

    def get_market(self, market_id):
        market = self.markets[market_id]
        if isinstance(market, Exception):
            raise market
        return market


class FakeJournal:
    def __init__(self):
        self.rows = []

    def record(self, signal, mode, filled):
        self.rows.append((signal, mode, filled))


def test_resolved_payouts_needs_a_closed_market_with_one_winner():
    assert resolved_payouts(binary_market("yes")) == {"yes": 1.0, "no": 0.0}
    assert resolved_payouts(binary_market("yes", closed=False)) is None
    assert resolved_payouts(binary_market(None)) is None  # closed, not resolved yet
    two_winners = {"closed": True, "tokens": [{"token_id": "a", "winner": True}, {"token_id": "b", "winner": True}]}
    assert resolved_payouts(two_winners) is None
    assert resolved_payouts(None) is None


def test_settles_resolved_markets_and_leaves_the_rest():
    risk = RiskManager(RiskConfig())
    # hedged arbitrage in mkt1 (resolved), an open market, and one the API fails on
    risk.record_open("mkt1", "yes", "YES", 10.0, 4.87, outcome_count=2)
    risk.record_open("mkt1", "no", "NO", 10.0, 5.07, outcome_count=2)
    risk.record_open("mkt2", "x", "YES", 5.0, 2.0)
    risk.record_open("mkt3", "z", "YES", 5.0, 2.0)
    client = FakeClient(
        {"mkt1": binary_market("no"), "mkt2": {"closed": False, "tokens": []}, "mkt3": RuntimeError("api down")}
    )
    journal = FakeJournal()

    settled = settle_resolved_markets(client, risk, journal, mode="paper")

    assert [market_id for market_id, _ in settled] == ["mkt1"]
    assert abs(settled[0][1] - (10.0 - 9.94)) < 1e-9  # $10 payout on $9.94 cost
    assert set(risk.positions) == {"x", "z"}
    assert abs(risk.realized_pnl_today - 0.06) < 1e-9
    assert [(s.strategy, s.side, s.limit_price, filled) for s, _, filled in journal.rows] == [
        ("settlement", "SELL", 0.0, True),
        ("settlement", "SELL", 1.0, True),
    ]
