import json

import pytest

from updown.broker import PaperBroker
from updown.config import MakerConfig, SizingConfig, UpDownSettings
from updown.engine import UpDownEngine
from updown.feeds import PriceHistory
from updown.lead_feed import binance_stream_url, parse_binance, parse_hyperliquid
from updown.maker import LeadGuard
from updown.strategy import DOWN, UP

from tests.test_updown_maker import T0, ScriptedGateway


def test_parses_binance_agg_trades_and_hyperliquid_mids():
    raw = json.dumps({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", "p": "84500.10", "T": 1}})
    assert parse_binance(raw) == ("btc", 84500.10)
    assert parse_binance("junk") is None and parse_binance(json.dumps({"data": {"s": "XYZUSDT", "p": "1"}})) is None
    hl = json.dumps({"channel": "allMids", "data": {"mids": {"HYPE": "91.2", "BTC": "1"}}})
    assert parse_hyperliquid(hl, ["hype"]) == [("hype", 91.2)]
    assert parse_hyperliquid(json.dumps({"channel": "pong"}), ["hype"]) == []
    assert binance_stream_url(["btc", "doge"]).endswith("streams=btcusdt@aggTrade/dogeusdt@aggTrade")


def test_lead_guard_trips_on_a_sharp_move_only_after_the_latency():
    cfg = MakerConfig(lead_move_z=2.5, lead_window_s=3, lead_cooldown_s=10, lead_latency_s=0.5)
    g, h = LeadGuard(cfg), PriceHistory()
    for i in range(50):
        h.add("btc", 100 + i * 0.1, 100.0)
    sigma = 1e-4
    assert not g.check(h, "btc", 105.0, sigma)  # flat: nothing
    h.add("btc", 105.1, 100.2)  # +0.2% in a blink, ~11 sigma over 3s
    assert not g.check(h, "btc", 105.3, sigma)  # seen only 0.2s ago: too late to cancel
    assert g.check(h, "btc", 105.7, sigma)
    assert g.check(h, "btc", 114.0, sigma)  # cooldown
    assert g.trips["btc"] == 1


class MovingGateway(ScriptedGateway):
    """At 60s the Up side collapses (Binance dumped a second earlier)."""

    def books(self, tokens):
        el = (self.now - T0) % 300
        from updown.strategy import Book

        out = {}
        for t in tokens:
            if t.startswith("up") and el >= 60:
                out[t] = Book.from_raw([(0.09, 100)], [(0.10, 100)])
            elif t.startswith("down") and el >= 60:
                out[t] = Book.from_raw([(0.89, 100)], [(0.91, 100)])
            else:
                out[t] = Book.from_raw([(0.49, 100)], [(0.51, 100)])
        return out


def run(lead: bool):
    s = UpDownSettings(wallet=None, assets=["btc"], mode="maker")
    s.sizing = SizingConfig(bankroll_usd=1000)
    s.maker = MakerConfig(half_spread=0.03, quote_shares=10, fast_move_z=99, vol_spread_mult=0, lead_guard=lead)
    gw = MovingGateway(DOWN)
    history, lead_history = PriceHistory(), PriceHistory()
    engine = UpDownEngine(s, gw, PaperBroker(), history, journal=None, settle_fallback_s=30, lead_history=lead_history)
    engine.vol["btc"].seed(1e-4)
    for t in range(T0 - 65, T0 + 80):
        gw.now = t
        history.add("btc", float(t), 100.0)
        lead_history.add("btc", float(t) - 0.5, 99.5 if t >= T0 + 59 else 100.0)
        engine.tick(float(t))
    return engine


def test_early_warning_pulls_the_bid_before_the_book_collapses_onto_it():
    unprotected = [e for e in run(lead=False).events if e["kind"] == "BUY"]
    assert any(e["outcome"] == UP and e["price"] > 0.4 for e in unprotected)  # stale Up bid got hit
    engine = run(lead=True)
    assert not [e for e in engine.events if e["kind"] == "BUY"]
    assert engine.lead_guard.trips["btc"] == 1


class SlowGateway(ScriptedGateway):
    """Discovery and resolution each take 0.3s, like real HTTP calls."""

    def discover(self, asset, start):
        import time
        time.sleep(0.3)
        return super().discover(asset, start)

    def resolution(self, wm):
        import time
        time.sleep(0.3)
        return self.winner


def test_background_io_keeps_ticks_fast_and_still_settles():
    import time

    s = UpDownSettings(wallet=None, assets=["btc"], mode="maker")
    s.sizing = SizingConfig(bankroll_usd=1000)
    s.maker = MakerConfig(half_spread=0.03, quote_shares=10, fast_move_z=99, vol_spread_mult=0, lead_guard=False)
    gw = SlowGateway(UP)
    history = PriceHistory()
    engine = UpDownEngine(s, gw, PaperBroker(), history, journal=None, settle_fallback_s=10_000, background_io=True)
    engine.vol["btc"].seed(1e-4)
    slowest = 0.0
    t = T0 - 65
    while t < T0 + 300 + 60 and engine.stats.windows_traded == 0:
        gw.now = t
        history.add("btc", float(t), 100.0)
        started = time.perf_counter()
        engine.tick(float(t))
        slowest = max(slowest, time.perf_counter() - started)
        if t in (T0 + 1, T0 + 303, T0 + 313):
            time.sleep(0.4)  # let the background lookup finish, like real time passing
        t += 1
    assert slowest < 0.2  # no tick waited on a 0.3s lookup
    assert engine.stats.windows_traded == 1  # found the market and settled via Polymarket


def test_one_coin_crashing_stops_new_positions_on_every_coin():
    s = UpDownSettings(wallet=None, assets=["btc", "eth"], mode="maker")
    s.sizing = SizingConfig(bankroll_usd=1000)
    s.maker = MakerConfig(half_spread=0.03, fast_move_z=99, vol_spread_mult=0, lead_global=True, lead_cooldown_s=10)
    gw = ScriptedGateway(UP)
    history, lead = PriceHistory(), PriceHistory()
    engine = UpDownEngine(s, gw, PaperBroker(), history, journal=None, lead_history=lead)
    for a in ("btc", "eth"):
        engine.vol[a].seed(1e-4)
    for t in range(T0 - 65, T0 + 40):
        gw.now = t
        for a in ("btc", "eth"):
            history.add(a, float(t), 100.0)
            lead.add(a, float(t) - 0.5, 100.0 if (a == "eth" or t < T0 + 35) else 99.0)  # BTC dumps at +35s
        engine.tick(float(t))
    eth = engine.windows[("eth", T0)]
    assert "another coin is moving fast" in eth.why
    assert not [tok for tok in engine.maker_broker.orders if tok.endswith(str(T0))]
