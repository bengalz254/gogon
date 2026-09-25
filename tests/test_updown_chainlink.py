import json

from updown.chainlink_feed import ChainlinkRTDSFeed, parse_message, subscribe_message
from updown.config import FeedConfig
from updown.feeds import BinanceFeed, PriceHistory, build_price_feeds

# The snapshot shape seen on the real socket (scripts/probe_rtds.py, Sep 2026).
SNAPSHOT = json.dumps({"payload": {"data": [
    {"timestamp": 1790303493000, "value": 84680.14332640175},
    {"timestamp": 1790303494000, "value": 84680.30443597712},
]}})


def test_subscribe_message_matches_the_confirmed_format():
    msg = json.loads(subscribe_message("btc/usd"))
    assert msg["action"] == "subscribe"
    sub = msg["subscriptions"][0]
    assert sub["topic"] == "crypto_prices_chainlink" and sub["type"] == "*"
    assert json.loads(sub["filters"]) == {"symbol": "btc/usd"}


def test_parses_snapshot_in_seconds():
    pts = parse_message(SNAPSHOT, "btc/usd")
    assert pts == [(1790303493.0, 84680.14332640175), (1790303494.0, 84680.30443597712)]


def test_parses_single_update_and_ignores_other_symbols_and_noise():
    upd = json.dumps({"topic": "crypto_prices_chainlink", "type": "update", "timestamp": 1790303499123,
                      "payload": {"symbol": "btc/usd", "timestamp": 1790303499000, "value": 84701.1}})
    assert parse_message(upd, "btc/usd") == [(1790303499.0, 84701.1)]
    assert parse_message(upd.replace("btc/usd", "eth/usd"), "btc/usd") == []
    for junk in ("PONG", "", "not json", json.dumps({"body": {"message": "x"}, "statusCode": 401})):
        assert parse_message(junk, "btc/usd") == []


def test_chainlink_is_the_default_source_with_candle_seeders_kept():
    assets = ["btc", "hype"]
    feeds, seeders = build_price_feeds(assets, FeedConfig(), PriceHistory())
    assert all(isinstance(feeds[a], ChainlinkRTDSFeed) for a in assets)
    assert isinstance(seeders["btc"], BinanceFeed)  # volatility still seeded from candles
    feeds, _ = build_price_feeds(assets, FeedConfig(source="binance"), PriceHistory())
    assert isinstance(feeds["btc"], BinanceFeed)


class _Primary:
    def __init__(self):
        self.last_seen = {}


def test_backup_only_learns_the_gap_while_chainlink_is_fresh():
    from updown.feeds import BackupSink

    now = [1000.0]
    hist, primary, ticks = PriceHistory(), _Primary(), []
    sink = BackupSink(hist, primary, stale_after_s=4, on_tick=lambda a, t, p: ticks.append(p), clock=lambda: now[0])
    hist.add("btc", 999.5, 100.0)
    primary.last_seen["btc"] = (999.8, 100.0)
    assert sink.add("btc", 1000.0, 99.0) is False  # Chainlink fresh: backup stays out
    assert hist.latest("btc") == (999.5, 100.0) and ticks == []
    now[0] = 1010.0  # Chainlink silent for 10s
    assert sink.add("btc", 1010.0, 99.0) is True
    ts, price = hist.latest("btc")
    assert ts == 1010.0 and abs(price - 100.0) < 1e-9  # corrected by the learned gap
    assert ticks and sink.active["btc"]
    primary.last_seen["btc"] = (1011.0, 100.5)  # Chainlink back
    now[0] = 1011.5
    assert sink.add("btc", 1011.5, 99.5) is False and not sink.active["btc"]


def test_chainlink_mode_starts_backup_feeds():
    feeds, seeders = build_price_feeds(["btc", "hype"], FeedConfig(), PriceHistory())
    kinds = {type(f).__name__ for f in feeds.values()}
    assert kinds == {"ChainlinkRTDSFeed", "BinanceFeed", "HyperliquidFeed"}
    feeds, _ = build_price_feeds(["btc"], FeedConfig(backup=False), PriceHistory())
    assert {type(f).__name__ for f in feeds.values()} == {"ChainlinkRTDSFeed"}
