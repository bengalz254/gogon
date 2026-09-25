"""Polymarket Real-Time Data Socket (RTDS): Chainlink prices -- the same
source Up/Down windows settle on -- plus Polymarket's relay of Binance
prices (topic crypto_prices), usable as the fast CEX indicator.

Behaviour seen on the real socket (Sep 2026, from this repo's other Up/Down
branch and its scripts/probe_rtds.py):
- The first reply is a snapshot of the last couple of minutes, with neither
  `topic` nor `symbol`: {"payload": {"data": [{"timestamp", "value"}, ...]}}.
- Live updates are tagged: {"topic", "type", "payload": {"symbol", "timestamp", "value"}}.
- With some subscribe styles the server sends the snapshot and then goes
  silent while still answering PING.

So: one connection per symbol (an untagged snapshot is unambiguous), and a
connection that shows no live update before `stale_s` is reconnected with
the next subscribe style until one streams; that style is then kept.
"""
from __future__ import annotations

import json
import logging

from bot.updown.events import CexTick, OracleTick
from bot.updown.feeds.base import WsFeed, decode_frame

logger = logging.getLogger("polybot.updown.feeds.rtds")

CHAINLINK_TOPIC = "crypto_prices_chainlink"
CEX_TOPIC = "crypto_prices"
CHAINLINK_VARIANTS = ("compact", "nofilter", "spaced")
CEX_VARIANTS = ("plain", "nofilter")


def subscribe_message(topic: str, symbol: str, variant: str) -> dict:
    if topic == CHAINLINK_TOPIC:
        sub = {"topic": topic, "type": "*"}
        if variant == "compact":
            sub["filters"] = json.dumps({"symbol": symbol}, separators=(",", ":"))
        elif variant == "spaced":
            sub["filters"] = json.dumps({"symbol": symbol})
    else:
        sub = {"topic": topic, "type": "update"}
        if variant == "plain":
            sub["filters"] = symbol
    return {"action": "subscribe", "subscriptions": [sub]}


def _ts_seconds(value) -> float | None:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    return ts / 1000.0 if ts > 1e11 else ts


def parse_points(obj, topic: str, symbol: str, require_tag: bool = False) -> list[tuple[float, float]]:
    """(timestamp_s, price) points for `symbol` in one RTDS message, oldest first.

    Accepts the untagged snapshot and tagged single updates. Points tagged
    with another symbol or topic are ignored; with `require_tag` (unfiltered
    subscription: every coin on one socket) untagged points are too.
    """
    if isinstance(obj, list):
        out = []
        for item in obj:
            out.extend(parse_points(item, topic, symbol, require_tag))
        return sorted(out)
    if not isinstance(obj, dict):
        return []
    if obj.get("topic") not in (None, topic):
        return []
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return []
    tagged = payload.get("symbol")
    if tagged and str(tagged).lower() != symbol:
        return []
    if require_tag and not tagged:
        return []
    rows = payload.get("data") if isinstance(payload.get("data"), list) else [payload]
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = _ts_seconds(row.get("timestamp", obj.get("timestamp")))
        try:
            price = float(row.get("value", row.get("price")))
        except (TypeError, ValueError):
            continue
        if ts is not None and price > 0:
            out.append((ts, price))
    return sorted(out)


class RtdsSymbolFeed(WsFeed):
    """One RTDS socket for one symbol of one topic."""

    ping_text = "PING"

    def __init__(self, url: str, topic: str, symbol: str, asset: str, emit, *, ping_s: float = 5.0,
                 stale_s: float = 10.0, on_status=None):
        super().__init__(on_status)
        self._url = url
        self.topic = topic
        self.symbol = symbol.lower()
        self.asset = asset
        self.emit = emit
        self.event_cls = OracleTick if topic == CHAINLINK_TOPIC else CexTick
        self.variants = CHAINLINK_VARIANTS if topic == CHAINLINK_TOPIC else CEX_VARIANTS
        self.name = f"{'oracle' if topic == CHAINLINK_TOPIC else 'cex'}:{asset}"
        self.ping_interval = ping_s
        self.idle_timeout = stale_s
        self.variant_index = 0
        self.streaming = False  # a style has delivered live updates: keep it
        self.max_ts = 0.0
        self._msgs = 0  # messages with new prices on the current connection
        self._updates = 0  # ... after the first one (usually the snapshot)
        self._silent_connections = 0
        self._errors_logged = 0

    @property
    def variant(self) -> str:
        return self.variants[self.variant_index % len(self.variants)]

    def url(self) -> str:
        return self._url

    async def on_open(self, ws) -> None:
        self._msgs = self._updates = 0
        await ws.send(json.dumps(subscribe_message(self.topic, self.symbol, self.variant)))

    def on_message(self, raw) -> bool:
        text = decode_frame(raw)
        if text is None:
            return False
        obj = json.loads(text)
        points = parse_points(obj, self.topic, self.symbol, require_tag=self.variant == "nofilter")
        if not points:
            if isinstance(obj, dict) and "statusCode" in obj and self._errors_logged < 5:
                self._errors_logged += 1
                logger.warning("%s: RTDS said %.200s", self.name, text)
            return False
        new = False
        for ts, price in points:
            if ts > self.max_ts:
                self.max_ts = ts
                new = True
                self.emit(self.event_cls(self.asset, ts, price))
        if not new:
            return False  # e.g. a repeated snapshot: not a sign of life
        # Count messages that brought new prices; the first one on a
        # connection is usually the snapshot, the rest are live updates.
        self._msgs += 1
        if self._msgs > 1:
            self._updates += 1
            if self._updates == 3 and not self.streaming:
                self.streaming = True
                logger.info("%s: live updates streaming (subscribe style '%s')", self.name, self.variant)
        return True

    def on_close(self) -> None:
        if self._updates > 0:
            self._silent_connections = 0
            return
        self._silent_connections += 1
        if self.streaming and self._silent_connections >= 3:
            self.streaming = False  # the kept style stopped working: probe again
        if not self.streaming:
            old = self.variant
            self.variant_index += 1
            if self._should_log():
                logger.warning("%s: no live updates with subscribe style '%s'; trying '%s'",
                               self.name, old, self.variant)


def build_rtds_feeds(url: str, chainlink_map: dict, cex_map: dict, emit, *, ping_s: float, stale_s: float,
                     on_status=None) -> list[RtdsSymbolFeed]:
    """One feed per Chainlink symbol (settlement truth) and per CEX relay symbol.

    chainlink_map: {"btc/usd": "btc", ...}; cex_map: {"btcusdt": "btc", ...}.
    """
    feeds = [
        RtdsSymbolFeed(url, CHAINLINK_TOPIC, sym, asset, emit, ping_s=ping_s, stale_s=stale_s, on_status=on_status)
        for sym, asset in sorted(chainlink_map.items())
    ]
    feeds += [
        RtdsSymbolFeed(url, CEX_TOPIC, sym, asset, emit, ping_s=ping_s, stale_s=stale_s, on_status=on_status)
        for sym, asset in sorted(cex_map.items())
    ]
    return feeds
