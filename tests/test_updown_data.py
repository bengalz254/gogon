import asyncio
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
from bot.updown.feeds.clob_ws import ClobMarketFeed, parse_clob
from bot.updown.feeds.rtds import CEX_TOPIC, CHAINLINK_TOPIC, RtdsSymbolFeed, parse_points, subscribe_message
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
# Snapshot shape seen on the real RTDS socket (Sep 2026): no topic, no symbol.
REAL_SNAPSHOT = {"payload": {"data": [
    {"timestamp": 1790303493000, "value": 84680.14332640175},
    {"timestamp": 1790303494000, "value": 84680.30443597712},
]}}


def _update(ts_ms, value, symbol="btc/usd"):
    return {"topic": "crypto_prices_chainlink", "type": "update", "timestamp": ts_ms + 123,
            "payload": {"symbol": symbol, "timestamp": ts_ms, "value": value}}


def test_rtds_parses_untagged_snapshot_and_tagged_updates():
    assert parse_points(REAL_SNAPSHOT, CHAINLINK_TOPIC, "btc/usd") == [
        (1790303493.0, 84680.14332640175), (1790303494.0, 84680.30443597712)]
    # on an unfiltered socket (all coins) an untagged snapshot can't be attributed
    assert parse_points(REAL_SNAPSHOT, CHAINLINK_TOPIC, "btc/usd", require_tag=True) == []
    upd = _update(1790303499000, 84701.1)
    assert parse_points(upd, CHAINLINK_TOPIC, "btc/usd") == [(1790303499.0, 84701.1)]
    assert parse_points(upd, CHAINLINK_TOPIC, "eth/usd") == []
    assert parse_points(upd, CEX_TOPIC, "btc/usd") == []  # different topic
    relay = {"topic": "crypto_prices", "payload": {"symbol": "btcusdt", "timestamp": 1790303499500, "value": 84700.0}}
    assert parse_points(relay, CEX_TOPIC, "btcusdt") == [(1790303499.5, 84700.0)]
    assert parse_points({"body": {"message": "x"}, "statusCode": 401}, CHAINLINK_TOPIC, "btc/usd") == []


def test_rtds_subscribe_styles():
    compact = subscribe_message(CHAINLINK_TOPIC, "btc/usd", "compact")["subscriptions"][0]
    assert compact == {"topic": CHAINLINK_TOPIC, "type": "*", "filters": '{"symbol":"btc/usd"}'}
    assert subscribe_message(CHAINLINK_TOPIC, "btc/usd", "spaced")["subscriptions"][0]["filters"] == '{"symbol": "btc/usd"}'
    assert "filters" not in subscribe_message(CHAINLINK_TOPIC, "btc/usd", "nofilter")["subscriptions"][0]
    assert subscribe_message(CEX_TOPIC, "btcusdt", "plain")["subscriptions"][0]["filters"] == "btcusdt"


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(json.loads(text))


def test_rtds_feed_rotates_subscribe_style_until_live_updates_stream():
    got = []
    feed = RtdsSymbolFeed("ws://x", CHAINLINK_TOPIC, "btc/usd", "btc", got.append)
    snapshot = json.dumps(REAL_SNAPSHOT)

    # Connection 1 ("compact"): snapshot, then only PONG / empty frames.
    ws = _FakeWs()
    asyncio.run(feed.on_open(ws))
    assert ws.sent[0]["subscriptions"][0]["filters"] == '{"symbol":"btc/usd"}'
    assert feed.on_message(snapshot) is True
    assert feed.on_message("PONG") is False and feed.on_message("") is False
    assert feed.on_message(snapshot) is False  # nothing new: the idle watchdog keeps counting
    feed.on_close()
    assert feed.variant == "nofilter" and not feed.streaming

    # Connection 2 ("nofilter"): untagged snapshot ignored, tagged updates stream.
    ws = _FakeWs()
    asyncio.run(feed.on_open(ws))
    assert "filters" not in ws.sent[0]["subscriptions"][0]
    assert feed.on_message(snapshot) is False
    assert feed.on_message(json.dumps(_update(1790303500000, 84690.0, "eth/usd"))) is False
    for i in range(4):
        assert feed.on_message(json.dumps(_update(1790303500000 + i * 1000, 84690.0 + i))) is True
    assert feed.streaming
    feed.on_close()
    assert feed.variant == "nofilter"  # a style that streams is kept

    assert [e.ts for e in got] == [1790303493.0, 1790303494.0, 1790303500.0, 1790303501.0, 1790303502.0, 1790303503.0]
    assert all(isinstance(e, OracleTick) and e.asset == "btc" for e in got)


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


def test_clob_feed_swaps_books_without_reconnecting():
    feed = ClobMarketFeed("ws://x", lambda ev: None)
    feed.tokens = {"a", "b"}
    ws = _FakeWs()
    feed.ws = ws
    asyncio.run(feed.on_open(ws))
    asyncio.run(feed.set_tokens({"b", "c"}))
    assert ws.sent[1:] == [
        {"assets_ids": ["a"], "operation": "unsubscribe"},  # finished window: stop its traffic
        {"assets_ids": ["c"], "operation": "subscribe"},
    ]
    assert feed._subscribed == {"b", "c"}


def test_clob_socket_can_absorb_bursts():
    # With the library default (16 queued messages) Polymarket dropped the real
    # socket about once a minute with 1013 "slow consumer".
    assert ClobMarketFeed("ws://x", lambda ev: None).connect_kwargs()["max_queue"] >= 1024
    rtds = RtdsSymbolFeed("ws://x", CHAINLINK_TOPIC, "btc/usd", "btc", lambda ev: None)
    assert "max_queue" not in rtds.connect_kwargs()


def test_recorder_deletes_recordings_older_than_keep_days(tmp_path):
    for name in ("events-20260901-00.jsonl.gz", "events-20260920-10.jsonl.gz", "events-20260924-23.jsonl.gz", "notes.txt"):
        (tmp_path / name).write_bytes(b"")
    now = 1790330000.0  # 2026-09-25 09:53 UTC
    rec = EventRecorder(str(tmp_path), keep_days=3)
    rec.record(OracleTick("btc", now, 60000.0), now)  # opens this hour's file and prunes
    rec.close()
    left = sorted(p.name for p in tmp_path.iterdir())
    assert "events-20260901-00.jsonl.gz" not in left and "events-20260920-10.jsonl.gz" not in left
    assert "events-20260924-23.jsonl.gz" in left and "notes.txt" in left
    assert any(n.startswith("events-20260925-") for n in left)
