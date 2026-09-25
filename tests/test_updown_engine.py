import csv
import json

import pytest

from bot.updown.config import FairValueConfig, UpDownConfig
from bot.updown.engine import Engine
from bot.updown.events import BookSnapshot, FeedStatus, OfficialResolution, OracleTick, WindowListed
from bot.updown.journal import NullJournal, UpDownJournal
from bot.updown.orders import CancelOrder, PaperBroker, PlaceOrder
from bot.updown.risk import UpDownRisk
from bot.updown.strategies import Quote, Take
from updown_helpers import T0, spec


class Scripted:
    """Test strategy: returns whatever `fn(ctx)` returns."""

    name = "fair_value"

    def __init__(self, fn):
        self.cfg = FairValueConfig(max_usd_per_trade=100, max_usd_per_window=100)
        self.fn = fn

    def evaluate(self, ctx):
        return self.fn(ctx)


def make(fn=None, use_official=False, journal=None, gap_guard=0.0, rule="twap"):
    cfg = UpDownConfig()
    cfg.markets.assets = ["btc"]
    cfg.settlement.use_official = use_official
    cfg.settlement.rule = rule
    cfg.risk.bankroll_usd = 1000
    # Most tests script a market that deliberately disagrees with the model;
    # the model/market gap guard has its own test below.
    cfg.model.max_model_market_gap = gap_guard
    strategies = [Scripted(fn)] if fn else []
    eng = Engine(cfg, strategies, UpDownRisk(cfg.risk, now=T0 - 4000), journal or NullJournal(), mode="test")
    broker = PaperBroker(eng)
    sp = spec("btc")
    eng.handle(WindowListed(sp))
    return eng, broker, sp


def feed(eng, broker, ev, now):
    eng.handle(ev, now)
    for u in broker.on_event(ev, now):
        eng.on_order_update(u)


def run(eng, broker, sp, t_from, t_to, price, up=(0.60, 0.62), clock_ahead=0.0):
    """Feed one oracle tick + fresh books per second and step the engine.

    clock_ahead: how far the local clock runs ahead of the oracle's timestamps.
    """
    t = t_from
    while t <= t_to:
        feed(eng, broker, OracleTick("btc", t + 0.1 - clock_ahead, price(t)), t + 0.1)
        if sp.start - 5 <= t < sp.end:
            bid, ask = up
            feed(eng, broker, BookSnapshot(sp.up_token, t + 0.2, ((bid, 500.0),), ((ask, 500.0),)), t + 0.2)
            feed(eng, broker, BookSnapshot(sp.down_token, t + 0.2, ((round(1 - ask, 2), 500.0),), ((round(1 - bid, 2), 500.0),)), t + 0.2)
        for u in broker.execute(eng.step(t + 0.5), t + 0.5):
            eng.on_order_update(u)
        t += 1


def flat(t):
    return 100.0 + (0.0001 if int(t) % 2 else -0.0001)  # tiny wiggle so vol estimates exist


def test_price_to_beat_is_the_twap_of_the_minute_before_the_open():
    eng, broker, sp = make()
    run(eng, broker, sp, T0 - 1900, T0 + 5, lambda t: 100.0 + (t - T0) * 0.001)
    w = eng.windows[sp.window_id]
    # per-second samples at t0-59 .. t0 of a price rising 0.001/s: mean = 100 - 0.0295
    assert w.ptb == pytest.approx(99.9705, abs=1e-6)
    assert "TWAP" in w.ptb_source
    assert w.ptb_last == pytest.approx(100.0)  # kept for the settlement-rule check
    assert w.tradable_reason == ""


def test_last_price_rule_uses_the_price_at_the_open():
    eng, broker, sp = make(rule="last")
    run(eng, broker, sp, T0 - 1900, T0 + 5, lambda t: 100.0 + (t - T0) * 0.001)
    assert eng.windows[sp.window_id].ptb == pytest.approx(100.0)


def test_no_price_to_beat_without_the_minute_before_the_open():
    eng, broker, sp = make(lambda ctx: [Take("Up", 0.99, 0.99, 0.3, "always")])
    run(eng, broker, sp, T0 - 20, T0 + 60, flat)  # only 20s of history before the open
    w = eng.windows[sp.window_id]
    assert w.ptb is None and "price_to_beat" in w.tradable_reason
    assert eng.stats["takes"] == 0


def test_model_market_gap_guard_stays_out():
    def after_jump(ctx):
        return [Take("Up", 0.62, 0.9, 0.25, "x")] if ctx.model.elapsed >= 70 else []

    eng, broker, sp = make(after_jump, gap_guard=0.30)
    # price runs well above the strike, but the market still says ~0.61
    run(eng, broker, sp, T0 - 1900, T0 + 130, lambda t: flat(t) + (0.5 if t >= T0 + 60 else 0.0))
    w = eng.windows[sp.window_id]
    assert "market" in w.tradable_reason and "gap" in w.tradable_reason
    assert eng.stats["takes"] == 0


def test_missed_open_is_never_traded():
    eng, broker, sp = make(lambda ctx: [Take("Up", 0.99, 0.99, 0.3, "always")])
    run(eng, broker, sp, T0 + 30, T0 + 120, flat)  # first data 30s after the open
    w = eng.windows[sp.window_id]
    assert w.ptb is None and "price_to_beat" in w.tradable_reason
    assert eng.stats["takes"] == 0


def test_full_lifecycle_books_correct_pnl(tmp_path):
    journal = UpDownJournal(str(tmp_path), str(tmp_path / "trades.csv"), mode="test")
    once = {"done": False}

    def buy_up_once(ctx):
        if once["done"] or ctx.model.elapsed < 100:
            return []
        once["done"] = True
        return [Take("Up", 0.62, 0.9, 0.25, "test entry")]

    eng, broker, sp = make(buy_up_once, journal=journal)
    run(eng, broker, sp, T0 - 1900, T0 + 330, lambda t: flat(t) + (0.5 if t >= T0 + 60 else 0.0))
    w = eng.windows[sp.window_id]
    h = w.holdings[("fair_value", "Up")]
    assert h.shares > 0 and w.booked and w.booked_winner == "Up"
    assert w.pnl["fair_value"] == pytest.approx(h.shares - h.cost)
    assert eng.risk.realized_pnl_today == pytest.approx(h.shares - h.cost)
    journal.close()
    rows = list(csv.DictReader(open(tmp_path / "trades.csv")))
    assert [r["side"] for r in rows] == ["BUY", "SETTLE"]
    settles = list(csv.DictReader(open(tmp_path / "updown_settlements.csv")))
    assert {r["event"] for r in settles} >= {"provisional", "booked"}
    assert (tmp_path / "updown_snapshots.jsonl").stat().st_size > 0
    assert (tmp_path / "updown_decisions.jsonl").stat().st_size > 0


def test_official_result_overrides_and_corrects_pnl():
    fired = {"n": 0}

    def buy_up(ctx):
        if fired["n"] or ctx.model.elapsed < 100:
            return []
        fired["n"] += 1
        return [Take("Up", 0.62, 0.9, 0.25, "x")]

    eng, broker, sp = make(buy_up)
    run(eng, broker, sp, T0 - 1900, T0 + 310, lambda t: flat(t) + (0.5 if t >= T0 + 60 else 0.0))
    w = eng.windows[sp.window_id]
    h = w.holdings[("fair_value", "Up")]
    assert w.booked_winner == "Up"
    eng.handle(OfficialResolution(sp.window_id, "Down"))  # the exchange disagrees
    assert w.booked_winner == "Down"
    assert w.pnl["fair_value"] == pytest.approx(-h.cost)
    assert eng.risk.realized_pnl_today == pytest.approx(-h.cost)


def test_waits_for_official_result_when_configured():
    fired = {"n": 0}

    def buy_up(ctx):
        if fired["n"] or ctx.model.elapsed < 100:
            return []
        fired["n"] += 1
        return [Take("Up", 0.62, 0.9, 0.25, "x")]

    eng, broker, sp = make(buy_up, use_official=True)
    run(eng, broker, sp, T0 - 1900, T0 + 320, lambda t: flat(t) + (0.5 if t >= T0 + 60 else 0.0))
    w = eng.windows[sp.window_id]
    assert not w.booked and w.provisional_winner == "Up"
    assert eng.risk.pending_pnl > 0  # counted for the kill switch meanwhile
    eng.handle(OfficialResolution(sp.window_id, "Up"))
    eng.step(T0 + 321)
    assert w.booked and eng.risk.realized_pnl_today > 0


def test_quotes_are_reconciled_and_cancelled_when_withdrawn():
    state = {"quote": True}

    def quoting(ctx):
        return [Quote("bid", "Up", 0.55, 0.7, "maker test")] if state["quote"] else []

    eng, broker, sp = make(quoting)
    run(eng, broker, sp, T0 - 1900, T0 + 100, flat)
    live = list(eng.orders.live(sp.window_id))
    assert len(live) == 1 and live[0].req.tif == "GTC" and live[0].req.post_only
    placed = eng.stats["quotes"]
    run(eng, broker, sp, T0 + 101, T0 + 110, flat)
    assert eng.stats["quotes"] == placed  # unchanged quote is left alone
    state["quote"] = False
    actions = eng.step(T0 + 111)
    assert any(isinstance(a, CancelOrder) for a in actions)


def test_stale_oracle_and_feed_outage_stop_trading():
    eng, broker, sp = make(lambda ctx: [Quote("bid", "Up", 0.55, 0.7, "q")])
    run(eng, broker, sp, T0 - 1900, T0 + 60, flat)
    w = eng.windows[sp.window_id]
    assert w.tradable_reason == ""
    actions = eng.step(T0 + 70)  # no oracle tick for ~10s
    assert "stale" in w.tradable_reason
    assert any(isinstance(a, CancelOrder) for a in actions)
    eng.handle(FeedStatus("clob", T0 + 71, False, "boom"))
    run(eng, broker, sp, T0 + 72, T0 + 75, flat)
    assert "clob feed down" in w.tradable_reason
    eng.handle(FeedStatus("clob", T0 + 76, True))
    run(eng, broker, sp, T0 + 76, T0 + 80, flat)
    assert w.tradable_reason == ""


def test_early_taker_ban_applies_inside_engine():
    eng, broker, sp = make(lambda ctx: [Take("Up", 0.55, 0.9, 0.3, "early")])
    run(eng, broker, sp, T0 - 1900, T0 + 20, flat, up=(0.50, 0.52))
    assert eng.stats["takes"] == 0
    assert not any(isinstance(a, PlaceOrder) for a in eng.step(T0 + 21))


def test_local_clock_skew_does_not_make_a_live_oracle_look_stale():
    eng, broker, sp = make()
    run(eng, broker, sp, T0 - 1900, T0 + 60, flat, clock_ahead=10.0)
    w = eng.windows[sp.window_id]
    assert w.ptb is not None  # locked once the oracle's own clock reached t=0
    assert w.tradable_reason == ""
    eng.step(T0 + 70)  # ...but ten seconds without a new price is stale
    assert "stale" in w.tradable_reason


def test_status_snapshot_is_strict_json_for_the_dashboard():
    fired = {"n": 0}

    def buy_up(ctx):
        if fired["n"] or ctx.model.elapsed < 100:
            return []
        fired["n"] += 1
        return [Take("Up", 0.62, 0.9, 0.25, "x")]

    eng, broker, sp = make(buy_up)
    run(eng, broker, sp, T0 - 1900, T0 + 200, lambda t: flat(t) + (0.5 if t >= T0 + 60 else 0.0))
    snap = eng.status_snapshot(T0 + 200.5)
    text = json.dumps(snap, allow_nan=False)  # the browser's JSON.parse rejects NaN/Infinity
    w = json.loads(text)["windows"][0]
    assert w["phase"] == "live" and w["model"]["p_up"] > 0.5 and w["history"]
    assert w["holdings"][0]["outcome"] == "Up" and w["market"]["up_ask"] == 0.62
    assert set(snap["risk"]) >= {"realized_today", "pending", "kill_switch", "cooldowns"}
