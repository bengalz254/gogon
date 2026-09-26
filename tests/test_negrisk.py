import json
import sqlite3

from bot.config import FeeConfig, NegRiskArbitrageConfig, RiskConfig
from bot.execution import OrderExecutor
from bot.fees import FeeModel, FeeSchedule
from bot.gamma import fetch_negrisk_events, parse_negrisk_event
from bot.orderbook import BookLevel
from bot.risk import RiskManager
from bot.state import StateStore
from bot.strategies.negrisk_arbitrage import NegRiskArbitrageStrategy


def outcome(i, **overrides):
    m = {
        "conditionId": f"0xc{i}",
        "question": f"Will candidate {i} win?",
        "groupItemTitle": f"Candidate {i}",
        "clobTokenIds": json.dumps([f"yes{i}", f"no{i}"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "negRiskOther": False,
    }
    m.update(overrides)
    return m


def event(n=3, **overrides):
    e = {
        "id": "42",
        "title": "Who will win?",
        "negRisk": True,
        "enableNegRisk": True,
        "negRiskAugmented": False,
        "negRiskMarketID": "0xset",
        "tags": [{"label": "Politics"}],
        "markets": [outcome(i) for i in range(n)],
    }
    e.update(overrides)
    return e


# -- which events are safe ----------------------------------------------------------


def test_complete_event_is_accepted():
    parsed, reason = parse_negrisk_event(event())
    assert reason == "" and parsed.set_id == "0xset"
    assert [m.tokens[0].token_id for m in parsed.outcomes] == ["yes0", "yes1", "yes2"]
    assert parsed.outcomes[0].question == "Candidate 0" and parsed.outcomes[0].tags == ["Politics"]


def test_unsafe_events_are_rejected():
    cases = {
        "not negRisk": event(negRisk=False),
        "augmented": event(negRiskAugmented=True),
        "augmentation not stated": {k: v for k, v in event().items() if k != "negRiskAugmented"},
        "Other placeholder": event(markets=[outcome(0), outcome(1, negRiskOther=True)]),
        "closed outcome": event(markets=[outcome(0), outcome(1, closed=True), outcome(2)]),
        "not accepting": event(markets=[outcome(0), outcome(1, acceptingOrders=False)]),
        "odd tokens": event(markets=[outcome(0), outcome(1, outcomes=json.dumps(["A", "B"]))]),
        "single outcome": event(n=1),
        "duplicate": event(markets=[outcome(0), outcome(0)]),
    }
    for name, raw in cases.items():
        parsed, reason = parse_negrisk_event(raw)
        assert parsed is None and reason, name
    # enableNegRisk explicitly off also rules out augmentation
    parsed, _ = parse_negrisk_event({**{k: v for k, v in event().items() if k != "negRiskAugmented"}, "enableNegRisk": False})
    assert parsed is not None


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.offsets = []

    def get(self, url, params, timeout):
        self.offsets.append(params["offset"])
        return FakeResponse(self.pages[params["offset"] // params["limit"]])


def test_fetch_pages_filters_and_caps():
    big = event(n=12)
    big["id"] = "big"
    pages = [[event(), event(negRiskAugmented=True), big, {"negRisk": False}], []]
    events = fetch_negrisk_events(max_events=5, max_outcomes=10, page_size=4, session=FakeSession(pages))
    assert [e.event_id for e in events] == ["42"]


# -- strategy ---------------------------------------------------------------------------


def make_strategy(max_position_usd=100.0, min_edge=0.02, fees=None):
    risk = RiskManager(
        RiskConfig(max_position_usd=max_position_usd, max_total_exposure_usd=500, max_daily_loss_usd=100, min_order_size_usd=1)
    )
    cfg = NegRiskArbitrageConfig(enabled=True, min_edge=min_edge, fee_buffer=0.005)
    return NegRiskArbitrageStrategy(cfg, risk, fees or FeeModel.zero()), risk


def books(asks, size=500):
    return {f"yes{i}": BookLevel(a - 0.01, a, size, size) for i, a in enumerate(asks)}


def test_buys_every_outcome_when_the_set_costs_under_a_dollar():
    strat, _ = make_strategy()
    ev, _ = parse_negrisk_event(event())
    b = books([0.30, 0.30, 0.34])  # 0.94: 6c discount
    signals = strat.generate_signals(ev, lambda t: b[t])
    assert len(signals) == 3 and len({s.group_id for s in signals}) == 1
    assert {s.set_id for s in signals} == {"0xset"} and {s.outcome_count for s in signals} == {3}
    assert {s.size_shares for s in signals} == {round(int(100 / 0.94 * 100) / 100, 2)}


def test_no_trade_when_fees_eat_the_discount_or_a_leg_is_missing():
    strat, _ = make_strategy(fees=FeeModel(FeeConfig()))  # Politics tag -> 0.04 rate
    ev, _ = parse_negrisk_event(event())
    # 0.97 with ~0.04-rate fees on three ~1/3 legs (~2.7c) leaves < 2c
    b = books([0.32, 0.32, 0.33])
    assert strat.generate_signals(ev, lambda t: b[t]) == []
    strat, _ = make_strategy()
    b = books([0.30, 0.30])
    b["yes2"] = BookLevel(0.3, None, 10, 0)
    assert strat.generate_signals(ev, lambda t: b[t]) == []


def test_cheap_leg_below_the_minimum_order_blocks_the_set():
    strat, _ = make_strategy(max_position_usd=20)
    ev, _ = parse_negrisk_event(event())
    b = books([0.02, 0.45, 0.45])  # 20 sets max -> the 0.02 leg is a $0.40 order
    assert strat.generate_signals(ev, lambda t: b[t]) == []


# -- risk & execution across markets -------------------------------------------------------


def test_set_is_risk_checked_and_valued_as_one_unit():
    strat, risk = make_strategy()
    ev, _ = parse_negrisk_event(event())
    b = books([0.30, 0.30, 0.34])
    executor = OrderExecutor(None, risk, type("J", (), {"record": lambda *a, **k: None})(), live=False)
    assert executor.execute_group(strat.generate_signals(ev, lambda t: b[t]))
    assert {p.market_id for p in risk.positions.values()} == {"0xc0", "0xc1", "0xc2"}
    assert abs(risk.market_exposure_usd("0xset") - risk.total_exposure_usd) < 1e-9
    # each leg's bid is below its ask, but the full set is worth $1 per share
    sets = risk.positions["yes0"].size
    marks = {f"yes{i}": 0.01 for i in range(3)}
    assert abs(risk.mark_to_market(marks) - (sets - risk.total_exposure_usd)) < 1e-9
    # held until resolution: not offered for merging
    assert risk.markets_with_complete_sets() == {}
    assert risk.record_merge("0xset") == []


def test_state_from_before_set_id_is_upgraded(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE positions (token_id TEXT PRIMARY KEY, market_id TEXT NOT NULL, outcome TEXT NOT NULL,
            size REAL NOT NULL, cost_usd REAL NOT NULL, opened_at TEXT NOT NULL, outcome_count INTEGER);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO positions VALUES ('tok', 'mkt', 'YES', 5.0, 2.5, '2026-09-01T00:00:00+00:00', 2);
        INSERT INTO meta VALUES ('day', '2026-09-01'), ('realized_pnl_today', '0.0');
        """
    )
    conn.commit()
    conn.close()
    state = StateStore(str(path)).load()
    assert state.positions[0].token_id == "tok" and state.positions[0].set_id is None


def test_fee_schedule_per_leg_is_used():
    per_market = {"0xc0": 0.0, "0xc1": 0.0, "0xc2": 0.05}

    class Fees(FeeModel):
        def schedule(self, market):
            return FeeSchedule(rate=per_market[market.condition_id])

    strat, _ = make_strategy(fees=Fees(FeeModel.zero().cfg))
    ev, _ = parse_negrisk_event(event())
    b = books([0.30, 0.30, 0.34])
    signals = strat.generate_signals(ev, lambda t: b[t])
    fees = {s.market_id: s.fee_usd for s in signals}
    assert fees["0xc0"] == 0 and fees["0xc2"] > 0
