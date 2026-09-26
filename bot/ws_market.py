"""Live order books from Polymarket's market WebSocket.

wss://ws-subscriptions-clob.polymarket.com/ws/market streams, per token:
  book              a full snapshot (bids/asks), sent on subscribe
  price_change      level updates, {"price_changes": [{asset_id, price, size,
                    side, ...}]} (older format: {asset_id, changes: [...]})
  last_trade_price  a trade: {asset_id, price, size, side}
  tick_size_change  {asset_id, new_tick_size}

A background thread keeps one connection open, sends "PING" every 10 s (the
server answers "PONG"), reconnects with backoff and re-subscribes when the
set of tokens changes. Books are dropped whenever the connection drops
(updates may have been missed) and are only served while the connection is
demonstrably alive, so callers fall back to REST rather than act on a
frozen book.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from bot.orderbook import OrderBook

logger = logging.getLogger("polybot.ws")

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_BUFFERED_TRADES = 10_000


@dataclass
class Trade:
    token_id: str
    price: float
    size: float
    side: str  # the taker's side ("BUY"/"SELL"), "" if not given
    received_at: float


def _default_connect(url: str, timeout: float):
    import websocket  # websocket-client

    return websocket.create_connection(url, timeout=timeout)


def _is_timeout(exc: Exception) -> bool:
    return type(exc).__name__ in ("WebSocketTimeoutException", "TimeoutError", "timeout")


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MarketFeed:
    def __init__(
        self,
        url: str = WS_MARKET_URL,
        stale_after: float = 30.0,
        ping_interval: float = 10.0,
        connect: Callable[[str, float], object] | None = None,
    ):
        self.url = url
        self.stale_after = stale_after
        self.ping_interval = ping_interval
        self._connect = connect or _default_connect
        self._lock = threading.Lock()
        self._books: dict[str, OrderBook] = {}
        self._trades: list[Trade] = []
        self._assets: frozenset[str] = frozenset()
        self._ws = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self._last_message_at = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="market-ws", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._close_socket()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def set_assets(self, token_ids: Iterable[str]) -> None:
        """Subscribe to exactly these tokens (reconnects if the set changed)."""
        assets = frozenset(str(t) for t in token_ids if t)
        if assets != self._assets:
            self._assets = assets
            self._close_socket()  # the run loop reconnects with the new set

    # -- reads (any thread) ------------------------------------------------
    def is_live(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.connected and now - self._last_message_at <= self.stale_after

    def book(self, token_id: str, now: float | None = None) -> OrderBook | None:
        """A copy of the token's book, or None if missing or the feed isn't live."""
        with self._lock:
            if not self.is_live(now):
                return None
            book = self._books.get(token_id)
            return book.copy() if book is not None else None

    def drain_trades(self) -> list[Trade]:
        with self._lock:
            trades, self._trades = self._trades, []
        return trades

    # -- message handling ----------------------------------------------------
    def handle_message(self, raw: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._last_message_at = now
        if not raw or raw in ("PONG", "PING"):
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        events = payload if isinstance(payload, list) else [payload]
        with self._lock:
            for event in events:
                if isinstance(event, dict):
                    self._apply(event, now)

    def _apply(self, event: dict, now: float) -> None:
        kind = event.get("event_type")
        if kind == "book":
            token_id = str(event.get("asset_id") or "")
            if not token_id:
                return
            book = OrderBook.from_snapshot(event, token_id=token_id, now=now)
            previous = self._books.get(token_id)
            if book.tick_size is None and previous is not None:
                book.tick_size = previous.tick_size
            self._books[token_id] = book
        elif kind == "price_change":
            changes = event.get("price_changes")
            if not isinstance(changes, list):  # older format: one token per event
                changes = [{**c, "asset_id": event.get("asset_id")} for c in event.get("changes") or []]
            for change in changes:
                if not isinstance(change, dict):
                    continue
                book = self._books.get(str(change.get("asset_id") or ""))
                price, size = _float(change.get("price")), _float(change.get("size"))
                # Deltas only apply on top of a snapshot; without one, wait for it.
                if book is not None and price is not None and size is not None:
                    book.apply_change(str(change.get("side", "")), price, size, now=now)
        elif kind == "last_trade_price":
            price, size = _float(event.get("price")), _float(event.get("size"))
            if price is not None and size is not None and event.get("asset_id"):
                self._trades.append(
                    Trade(str(event["asset_id"]), price, size, str(event.get("side", "")).upper(), now)
                )
                if len(self._trades) > MAX_BUFFERED_TRADES:
                    del self._trades[: len(self._trades) - MAX_BUFFERED_TRADES]
        elif kind == "tick_size_change":
            book = self._books.get(str(event.get("asset_id") or ""))
            tick = _float(event.get("new_tick_size"))
            if book is not None and tick is not None:
                book.tick_size = tick

    # -- connection loop (background thread) ------------------------------------
    def _close_socket(self) -> None:
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            assets = self._assets
            if not assets:
                self._stop.wait(1.0)
                continue
            resubscribe = False
            try:
                ws = self._connect(self.url, 10.0)
                self._ws = ws
                ws.send(json.dumps({"type": "market", "assets_ids": sorted(assets)}))
                self.connected = True
                self._last_message_at = time.time()
                logger.info("Market WebSocket connected (%d tokens)", len(assets))
                backoff = 1.0
                ws.settimeout(1.0)
                last_ping = time.monotonic()
                while not self._stop.is_set():
                    if assets != self._assets:
                        resubscribe = True
                        break
                    if time.monotonic() - last_ping >= self.ping_interval:
                        ws.send("PING")
                        last_ping = time.monotonic()
                    try:
                        message = ws.recv()
                    except Exception as exc:
                        if _is_timeout(exc):
                            continue
                        raise
                    if message is None or message == "":
                        break  # closed by the server
                    self.handle_message(message)
            except Exception as exc:
                if not self._stop.is_set() and assets == self._assets:
                    logger.warning("Market WebSocket error (%s); reconnecting in %.0fs", type(exc).__name__, backoff)
            finally:
                self.connected = False
                self._close_socket()
                self._ws = None
                with self._lock:
                    self._books.clear()  # updates may have been missed while down
            if self._stop.is_set():
                break
            if resubscribe or assets != self._assets:
                continue
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
        logger.info("Market WebSocket stopped")
