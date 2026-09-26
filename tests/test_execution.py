import random

from bot.config import RiskConfig
from bot.execution import OrderExecutor
from bot.risk import RiskManager
from bot.strategies.base import Signal


class FakeJournal:
    def __init__(self):
        self.rows = []

    def record(self, signal, mode, filled):
        self.rows.append((signal, mode, filled))


class FakeStore:
    def __init__(self):
        self.saved = []

    def save(self, state):
        self.saved.append(state)


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send(self, text, key=None, cooldown_s=0.0):
        self.messages.append(text)
        return True


class FakeLiveClient:
    """Live-mode client whose post_order results are scripted, call by call."""

    def __init__(self, results):
        self.results = list(results)
        self.posted = []

    def create_order(self, order_args):
        return order_args

    def post_order(self, order, order_type):
        self.posted.append(order)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_risk(**overrides):
    cfg = RiskConfig(
        max_position_usd=25.0, max_total_exposure_usd=200.0, max_daily_loss_usd=50.0, min_order_size_usd=1.0
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return RiskManager(cfg)


def leg(outcome, price, shares, fee=0.0, group="g1", market="mkt1", side="BUY"):
    return Signal(
        strategy="arbitrage",
        market_id=market,
        token_id=f"tok{outcome}",
        outcome=outcome,
        side=side,
        limit_price=price,
        size_shares=shares,
        size_usd=shares * price,
        reason="test",
        group_id=group,
        fee_usd=fee,
        outcome_count=2,
    )


def make_executor(risk, live=False, client=None):
    journal, store, notifier = FakeJournal(), FakeStore(), FakeNotifier()
    executor = OrderExecutor(client, risk, journal, live=live, store=store, notifier=notifier)
    return executor, journal, store, notifier


def test_pair_sized_exactly_to_the_cap_is_never_half_filled():
    # Regression: legs used to be risk-checked one at a time. Float noise in a
    # pair sized exactly to the $25 budget, or a cheap leg under the $1
    # minimum, rejected the second leg in about 1 of 5 cases — after the
    # first leg had already been bought.
    rng = random.Random(0)
    for _ in range(2000):
        a = round(rng.uniform(0.02, 0.96), 3)
        b = round(rng.uniform(0.01, 0.98 - a), 3)
        risk = make_risk()
        shares = risk.max_affordable_usd("mkt1") / (a + b)
        executor, _, _, _ = make_executor(risk)
        filled = executor.execute_group([leg("YES", a, shares), leg("NO", b, shares)])
        held = {"tokYES", "tokNO"} & set(risk.positions)
        assert held == ({"tokYES", "tokNO"} if filled else set())
        if min(shares * a, shares * b) >= 1.0:
            assert filled  # every valid pair goes through


def test_leg_below_the_minimum_order_rejects_the_whole_group():
    risk = make_risk()
    executor, journal, _, _ = make_executor(risk)
    assert not executor.execute_group([leg("YES", 0.95, 20), leg("NO", 0.02, 20)])  # NO leg is $0.40
    assert risk.positions == {}
    assert journal.rows == []


def test_group_over_the_cap_is_rejected_as_a_whole():
    risk = make_risk(max_position_usd=10.0)
    executor, journal, store, _ = make_executor(risk)
    assert not executor.execute_group([leg("YES", 0.50, 12), leg("NO", 0.45, 12)])  # $11.40 > $10
    assert risk.positions == {}
    assert journal.rows == []
    assert store.saved == []


def test_fees_count_toward_the_group_risk_check():
    risk = make_risk(max_position_usd=10.0)
    executor, _, _, _ = make_executor(risk)
    # $9.60 of shares + $0.50 of fees = $10.10 > $10
    assert not executor.execute_group([leg("YES", 0.50, 10, fee=0.25), leg("NO", 0.46, 10, fee=0.25)])
    assert risk.positions == {}


def test_paper_fill_adds_fees_to_cost_journals_and_persists():
    risk = make_risk()
    executor, journal, store, notifier = make_executor(risk)
    assert executor.execute_group([leg("YES", 0.47, 10, fee=0.17), leg("NO", 0.49, 10, fee=0.17)])
    assert abs(risk.positions["tokYES"].cost_usd - 4.87) < 1e-9
    assert abs(risk.positions["tokNO"].cost_usd - 5.07) < 1e-9
    assert [(mode, filled) for _, mode, filled in journal.rows] == [("paper", True), ("paper", True)]
    assert len(store.saved) == 2  # saved after each fill
    assert len(notifier.messages) == 1  # one message for the whole pair


def test_sell_proceeds_are_net_of_fees():
    risk = make_risk()
    risk.record_open("mkt1", "tokYES", "YES", size=10.0, cost_usd=4.0)
    executor, _, _, _ = make_executor(risk)
    assert executor.execute(leg("YES", 0.60, 10, fee=0.168, group=None, side="SELL"))
    assert abs(risk.realized_pnl_today - (6.0 - 0.168 - 4.0)) < 1e-9


def test_live_group_stops_after_the_first_failed_leg():
    risk = make_risk()
    client = FakeLiveClient([{"error": "order couldn't be fully filled"}])
    executor, journal, _, notifier = make_executor(risk, live=True, client=client)
    assert not executor.execute_group([leg("NO", 0.49, 10), leg("YES", 0.47, 10)])
    assert len(client.posted) == 1  # the second leg is never sent
    assert risk.positions == {}
    assert [filled for _, _, filled in journal.rows] == [False]
    assert notifier.messages == []  # nothing filled, nothing to alert


def test_live_half_filled_pair_is_tracked_and_alerted():
    risk = make_risk()
    client = FakeLiveClient([{"success": True, "orderID": "1"}, RuntimeError("timeout")])
    executor, journal, store, notifier = make_executor(risk, live=True, client=client)
    assert not executor.execute_group([leg("NO", 0.49, 10), leg("YES", 0.47, 10)])
    assert set(risk.positions) == {"tokNO"}  # the filled leg is still tracked
    assert [filled for _, _, filled in journal.rows] == [True, False]
    assert len(store.saved) == 1
    assert len(notifier.messages) == 1
    assert "TIDAK terlindung" in notifier.messages[0]


def test_execute_signals_keeps_groups_together_and_runs_singles():
    risk = make_risk()
    executor, journal, _, _ = make_executor(risk)
    signals = [leg("YES", 0.47, 5), leg("X", 0.30, 5, group=None, market="mkt2"), leg("NO", 0.49, 5)]
    executor.execute_signals(signals)
    assert set(risk.positions) == {"tokYES", "tokNO", "tokX"}
    assert [s.outcome for s, _, _ in journal.rows] == ["YES", "NO", "X"]


def test_live_fill_is_tracked_even_if_the_journal_write_fails():
    class BrokenJournal:
        def record(self, signal, mode, filled):
            raise OSError("No space left on device")

    risk = make_risk()
    client = FakeLiveClient([{"success": True, "orderID": "1"}])
    executor = OrderExecutor(client, risk, BrokenJournal(), live=True, store=FakeStore())
    assert executor.execute(leg("YES", 0.47, 10, group=None))
    assert set(risk.positions) == {"tokYES"}  # the real fill still counts against the caps
