"""Polymarket CLOB market websocket: full order books ("book"), level
updates ("price_change"), trade prints and tick-size changes for the
Up/Down tokens of the windows currently being watched."""
from __future__ import annotations

import json
import logging
import time

from bot.updown.events import BookLevel, BookSnapshot, TickSizeChange, TradePrint
from bot.updown.feeds.base import WsFeed, decode_frame

logger = logging.getLogger("polybot.updown.feeds.clob")


def _f(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ts(obj, recv_ts: float) -> float:
    ts = _f(obj.get("timestamp"))
    if ts is None:
        return recv_ts
    return ts / 1000.0 if ts > 1e11 else ts


def _levels(raw) -> tuple:
    out = []
    for lvl in raw or []:
        if isinstance(lvl, dict):
            p, s = _f(lvl.get("price")), _f(lvl.get("size"))
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            p, s = _f(lvl[0]), _f(lvl[1])
        else:
            continue
        if p is not None and s is not None:
            out.append((p, s))
    return tuple(out)


def parse_clob(obj, recv_ts: float | None = None) -> list:
    recv_ts = time.time() if recv_ts is None else recv_ts
    if isinstance(obj, list):
        out = []
        for item in obj:
            out.extend(parse_clob(item, recv_ts))
        return out
    if not isinstance(obj, dict):
        return []
    et = obj.get("event_type") or obj.get("type")
    ts = _ts(obj, recv_ts)
    if et == "book":
        token = str(obj.get("asset_id", ""))
        bids = _levels(obj.get("bids") if obj.get("bids") is not None else obj.get("buys"))
        asks = _levels(obj.get("asks") if obj.get("asks") is not None else obj.get("sells"))
        return [BookSnapshot(token, ts, bids, asks)] if token else []
    if et == "price_change":
        out = []
        if isinstance(obj.get("price_changes"), list):
            for ch in obj["price_changes"]:
                token = str(ch.get("asset_id", ""))
                price, size = _f(ch.get("price")), _f(ch.get("size"))
                side = str(ch.get("side", "")).upper()
                if token and price is not None and size is not None and side in ("BUY", "SELL"):
                    out.append(BookLevel(token, ts, side, price, size))
        else:
            token = str(obj.get("asset_id", ""))
            for ch in obj.get("changes") or []:
                price, size = _f(ch.get("price")), _f(ch.get("size"))
                side = str(ch.get("side", "")).upper()
                if token and price is not None and size is not None and side in ("BUY", "SELL"):
                    out.append(BookLevel(token, ts, side, price, size))
        return out
    if et == "last_trade_price":
        token = str(obj.get("asset_id", ""))
        price, size = _f(obj.get("price")), _f(obj.get("size"), 0.0)
        if token and price is not None:
            return [TradePrint(token, ts, price, size or 0.0, str(obj.get("side", "")).upper())]
        return []
    if et == "tick_size_change":
        token = str(obj.get("asset_id", ""))
        tick = _f(obj.get("new_tick_size"))
        return [TickSizeChange(token, ts, tick)] if token and tick else []
    return []


class ClobMarketFeed(WsFeed):
    name = "clob"
    ping_text = "PING"
    idle_timeout = 60.0
    # Book traffic is bursty (a snapshot per token right after subscribing, busy
    # quoting near the close). With the library's default of 16 queued messages
    # the real socket was dropped about once a minute with 1013 "slow consumer".
    max_queue = 4096

    def __init__(self, url: str, emit, ping_s: float = 10.0, dynamic_subscribe: bool = True, on_status=None):
        super().__init__(on_status)
        self._url = url
        self.emit = emit
        self.ping_interval = ping_s
        self.dynamic = dynamic_subscribe
        self.tokens: set = set()
        self._subscribed: set = set()

    def url(self) -> str:
        return self._url

    def ready(self) -> bool:
        return bool(self.tokens)

    async def on_open(self, ws) -> None:
        self._subscribed = set(self.tokens)
        await ws.send(json.dumps({"assets_ids": sorted(self._subscribed), "type": "market"}))

    async def set_tokens(self, tokens: set) -> None:
        """Watch exactly these tokens (new windows added, old ones dropped)."""
        tokens = set(tokens)
        if tokens == self.tokens:
            return
        self.tokens = tokens
        ws = self.ws  # the connection can drop while we await below
        if ws is None:
            return
        added, removed = tokens - self._subscribed, self._subscribed - tokens
        if self.dynamic:
            # Drop finished windows too: every extra book is more traffic to keep up with.
            if removed and await self.send_json({"assets_ids": sorted(removed), "operation": "unsubscribe"}):
                self._subscribed -= removed
            if not added:
                return
            if await self.send_json({"assets_ids": sorted(added), "operation": "subscribe"}):
                self._subscribed |= added
                return
        elif not added:
            return  # stale tokens are harmless until the next reconnect
        try:
            await ws.close()  # resubscribe everything on reconnect
        except Exception:
            pass

    def on_message(self, raw) -> bool:
        text = decode_frame(raw)
        if text is None:
            return False
        events = parse_clob(json.loads(text))
        for ev in events:
            self.emit(ev)
        return bool(events)
