import importlib.util
import json
import os

from bot.updown.discovery import build_slug, parse_price_to_beat, parse_resolution, parse_window, slot_starts
from bot.updown.events import (
    BookLevel,
    BookSnapshot,
    CexTick,
    OracleTick,
    TickSizeChange,
    TradePrint,
    WindowListed,
    event_from_dict,
    event_to_dict,
)
from bot.updown.feeds.cex import parse_binance, parse_bybit
from bot.updown.feeds.clob_ws import parse_clob
from bot.updown.feeds.rtds import parse_rtds, subscribe_message
from bot.updown.recorder import EventRecorder, read_events
from bot.updown.sim import SimConfig, run_simulation
from updown_helpers import spec

SLUG = "btc-updown-5m-1760000100"


def gamma_event(outcomes='["Up", "Down"]', closed=False, prices='["0.5", "0.5"]', meta=None):
    return {
        "slug": SLUG, "title": "Bitcoin Up or Down", "eventMetadata": meta,
        "markets": [{
            "conditionId": "0xabc", "question": "Bitcoin Up or Down - 5m", "outcomes": outcomes,
            "clobTokenIds": '["111", "222"]', "orderPriceMinTickSize": 0.01, "orderMinSize": 5,
            "negRisk": False, "closed": closed, "outcomePrices": prices,
        }],
    }


# -- discovery ------------------------------------------------------------------------------
def test_slugs_and_slots():
    assert slot_starts(1760000123.4, 300, 2) == [1760000100, 1760000400, 1760000700]
    assert build_slug("{asset}-updown-{interval}-{start}", "BTC", "5m", 1760000100) == SLUG


def test_parse_window_maps_tokens_by_outcome_name():
    w = parse_window(gamma_event(), asset="btc", interval_s=300, start=1760000100, slug=SLUG)
    assert (w.up_token, w.down_token, w.end, w.min_order_size) == ("111", "222", 1760000400.0, 5.0)
    flipped = parse_window(gamma_event(outcomes='["Down", "Up"]'), asset="btc", interval_s=300,
                           start=1760000100, slug=SLUG)
    assert (flipped.up_token, flipped.down_token) == ("222", "111")
    assert parse_window(gamma_event(outcomes='["Yes", "No"]'), asset="btc", interval_s=300,
                        start=1760000100, slug=SLUG) is None


def test_parse_resolution_and_price_to_beat():
    assert parse_resolution(gamma_event()) is None
    assert parse_resolution(gamma_event(closed=True, prices='["0", "1"]')) == "Down"
    assert parse_resolution(gamma_event(closed=True, prices='["1", "0"]')) == "Up"
    assert parse_price_to_beat(gamma_event(meta={"priceToBeat": 61234.5})) == 61234.5
    assert parse_price_to_beat(gamma_event()) is None


# -- feeds ----------------------------------------------------------------------------------
def test_parse_rtds_updates_and_backfill():
    cl, cx = {"btc/usd": "btc"}, {"btcusdt": "btc"}
    single = {"topic": "crypto_prices_chainlink", "type": "update", "timestamp": 1,
              "payload": {"symbol": "btc/usd", "timestamp": 1760000100500, "value": 61000.5}}
    assert parse_rtds(single, cl, cx) == [OracleTick("btc", 1760000100.5, 61000.5)]
    backfill = {"topic": "crypto_prices", "payload": {"symbol": "btcusdt", "data": [
        {"timestamp": 1760000102000, "value": 2.0}, {"timestamp": 1760000101000, "value": 1.0}]}}
    assert parse_rtds(backfill, cl, cx) == [CexTick("btc", 1760000101.0, 1.0), CexTick("btc", 1760000102.0, 2.0)]
    other = {"topic": "crypto_prices_chainlink", "payload": {"symbol": "eth/usd", "value": 1}}
    assert parse_rtds(other, cl, cx) == []
    sub = subscribe_message(["btc/usd"], ["btcusdt"])
    assert sub["subscriptions"][0]["filters"] == '{"symbol":"btc/usd"}'


def test_parse_clob_messages():
    book = {"event_type": "book", "asset_id": "111", "timestamp": "1760000100123",
            "bids": [{"price": "0.48", "size": "30"}], "asks": [{"price": "0.52", "size": "25"}]}
    assert parse_clob([book]) == [BookSnapshot("111", 1760000100.123, ((0.48, 30.0),), ((0.52, 25.0),))]
    new_fmt = {"event_type": "price_change", "market": "0xabc", "timestamp": "1760000100000", "price_changes": [
        {"asset_id": "111", "price": "0.5", "size": "200", "side": "BUY"},
        {"asset_id": "222", "price": "0.5", "size": "0", "side": "SELL"}]}
    assert parse_clob(new_fmt) == [BookLevel("111", 1760000100.0, "BUY", 0.5, 200.0),
                                   BookLevel("222", 1760000100.0, "SELL", 0.5, 0.0)]
    old_fmt = {"event_type": "price_change", "asset_id": "111", "timestamp": "1760000100000",
               "changes": [{"price": "0.4", "side": "SELL", "size": "5"}]}
    assert parse_clob(old_fmt) == [BookLevel("111", 1760000100.0, "SELL", 0.4, 5.0)]
    trade = {"event_type": "last_trade_price", "asset_id": "111", "price": "0.51", "size": "12", "side": "BUY",
             "timestamp": "1760000100000"}
    assert parse_clob(trade) == [TradePrint("111", 1760000100.0, 0.51, 12.0, "BUY")]
    tick = {"event_type": "tick_size_change", "asset_id": "111", "new_tick_size": "0.001", "timestamp": "1760000100000"}
    assert parse_clob(tick) == [TickSizeChange("111", 1760000100.0, 0.001)]


def test_parse_cex_trades():
    b = {"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", "p": "61000.1", "T": 1760000100000}}
    assert parse_binance(b, {"btcusdt": "btc"}) == [CexTick("btc", 1760000100.0, 61000.1)]
    y = {"topic": "publicTrade.BTCUSDT", "data": [{"T": 1760000100000, "s": "BTCUSDT", "p": "61000.2"}]}
    assert parse_bybit(y, {"btcusdt": "btc"}) == [CexTick("btc", 1760000100.0, 61000.2)]


# -- recording & replay ------------------------------------------------------------------------
def test_event_roundtrip(tmp_path):
    events = [OracleTick("btc", 1.5, 2.0), BookSnapshot("t", 2.0, ((0.4, 1.0),), ()), WindowListed(spec())]
    for ev in events:
        assert event_from_dict(json.loads(json.dumps(event_to_dict(ev)))) == ev
    rec = EventRecorder(str(tmp_path))
    for i, ev in enumerate(events):
        rec.record(ev, 1760000000.0 + i)
    rec.close()
    files = sorted(str(p) for p in tmp_path.iterdir())
    assert [ev for _, ev in read_events(files)] == events


def _load_backtest():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "updown_backtest.py")
    spec_ = importlib.util.spec_from_file_location("updown_backtest", path)
    mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(mod)
    return mod


def test_simulation_records_and_replays(tmp_path):
    sim = SimConfig(assets=["btc", "eth", "sol"], windows=3, warmup_s=900, seed=11)
    rec = EventRecorder(str(tmp_path / "rec"))
    result = run_simulation(sim, recorder=rec)
    rec.close()
    assert result.windows == 9
    assert result.settlement_mismatches == 0
    assert result.brier_model is not None
    for r in result.engine.results.values():  # every booked window: P&L == payout - cost
        assert set(r["pnl"]) <= {"fair_value", "late_certainty", "constellation"}

    from bot.updown.config import UpDownConfig
    cfg = UpDownConfig()
    cfg.markets.assets = sim.assets
    cfg.markets.interval = "300s"
    files = sorted(str(p) for p in (tmp_path / "rec").iterdir())
    engine, n = _load_backtest().replay(files, cfg)
    assert n > 1000
    assert len(engine.results) == 9
    assert all(r["official"] for r in engine.results.values())
