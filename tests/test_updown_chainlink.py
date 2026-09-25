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
