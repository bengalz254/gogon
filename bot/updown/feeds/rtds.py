"""Polymarket Real-Time Data Socket (RTDS): Chainlink prices -- the same
source Up/Down windows settle on -- plus Polymarket's relay of Binance
prices (topic crypto_prices), usable as the fast CEX indicator."""
from __future__ import annotations

import json
import logging
import time

from bot.updown.events import CexTick, OracleTick
from bot.updown.feeds.base import WsFeed

logger = logging.getLogger("polybot.updown.feeds.rtds")

CHAINLINK_TOPIC = "crypto_prices_chainlink"
CEX_TOPIC = "crypto_prices"


def _ts_seconds(value, fallback: float) -> float:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return fallback
    return ts / 1000.0 if ts > 1e11 else ts


def parse_rtds(obj, chainlink_map: dict, cex_map: dict, recv_ts: float | None = None) -> list:
    """Parse one RTDS message into OracleTick / CexTick events.

    chainlink_map: {"btc/usd": "btc", ...}; cex_map: {"btcusdt": "btc", ...}.
    Handles single updates and backfill payloads ({"symbol", "data": [...]}).
    """
    recv_ts = time.time() if recv_ts is None else recv_ts
    if isinstance(obj, list):
        out = []
        for item in obj:
            out.extend(parse_rtds(item, chainlink_map, cex_map, recv_ts))
        return out
    if not isinstance(obj, dict):
        return []
    topic = obj.get("topic")
    if topic == CHAINLINK_TOPIC:
        mapping, cls = chainlink_map, OracleTick
    elif topic == CEX_TOPIC:
        mapping, cls = cex_map, CexTick
    else:
        return []
    payload = obj.get("payload") or {}
    if not isinstance(payload, dict):
        return []
    symbol = str(payload.get("symbol", "")).lower()
    asset = mapping.get(symbol)
    if asset is None:
        return []
    points = payload.get("data")
    if not isinstance(points, list):
        points = [payload]
    out = []
    for p in points:
        if not isinstance(p, dict):
            continue
        value = p.get("value", p.get("price"))
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        ts = _ts_seconds(p.get("timestamp", obj.get("timestamp")), recv_ts)
        out.append(cls(asset, ts, price))
    out.sort(key=lambda e: e.ts)
    return out


def subscribe_message(chainlink_symbols: list, cex_symbols: list) -> dict:
    subs = [
        {"topic": CHAINLINK_TOPIC, "type": "*", "filters": json.dumps({"symbol": sym}, separators=(",", ":"))}
        for sym in chainlink_symbols
    ]
    if cex_symbols:
        subs.append({"topic": CEX_TOPIC, "type": "update", "filters": ",".join(cex_symbols)})
    return {"action": "subscribe", "subscriptions": subs}


class RtdsFeed(WsFeed):
    name = "oracle"
    ping_text = "PING"
    idle_timeout = 20.0

    def __init__(self, url: str, chainlink_map: dict, cex_map: dict, emit, ping_s: float = 5.0, on_status=None):
        super().__init__(on_status)
        self._url = url
        self.chainlink_map = chainlink_map
        self.cex_map = cex_map
        self.emit = emit
        self.ping_interval = ping_s

    def url(self) -> str:
        return self._url

    async def on_open(self, ws) -> None:
        await ws.send(json.dumps(subscribe_message(sorted(self.chainlink_map), sorted(self.cex_map))))

    def on_message(self, raw) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        raw = raw.strip()
        if not raw or raw.upper() == "PONG" or raw[0] not in "[{":
            return
        for ev in parse_rtds(json.loads(raw), self.chainlink_map, self.cex_map):
            self.emit(ev)
