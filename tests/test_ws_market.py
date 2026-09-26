import json
import threading
import time

from bot.ws_market import MarketFeed


def book_event(token, bids, asks):
    return {
        "event_type": "book",
        "asset_id": token,
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        "tick_size": "0.01",
    }


def live_feed():
    feed = MarketFeed(stale_after=30)
    feed.connected = True
    return feed


def test_snapshot_then_new_format_price_changes():
    feed = live_feed()
    feed.handle_message(json.dumps([book_event("yes", [(0.45, 10)], [(0.50, 5)])]), now=100.0)
    feed.handle_message(
        json.dumps(
            {
                "event_type": "price_change",
                "market": "0xm",
                "price_changes": [
                    {"asset_id": "yes", "price": "0.46", "size": "3", "side": "BUY"},
                    {"asset_id": "yes", "price": "0.50", "size": "0", "side": "SELL"},
                    {"asset_id": "yes", "price": "0.52", "size": "8", "side": "SELL"},
                    {"asset_id": "unknown", "price": "0.1", "size": "1", "side": "BUY"},
                ],
            }
        ),
        now=101.0,
    )
    book = feed.book("yes", now=101.0)
    assert (book.best_bid, book.best_ask, book.tick_size) == (0.46, 0.52, 0.01)
    assert feed.book("unknown", now=101.0) is None  # deltas need a snapshot first


def test_old_format_price_change_and_tick_size_change():
    feed = live_feed()
    feed.handle_message(json.dumps(book_event("yes", [(0.45, 10)], [(0.50, 5)])), now=1.0)
    feed.handle_message(
        json.dumps({"event_type": "price_change", "asset_id": "yes", "changes": [{"price": "0.47", "size": "2", "side": "BUY"}]}),
        now=2.0,
    )
    feed.handle_message(json.dumps({"event_type": "tick_size_change", "asset_id": "yes", "new_tick_size": "0.001"}), now=3.0)
    book = feed.book("yes", now=3.0)
    assert (book.best_bid, book.tick_size) == (0.47, 0.001)


def test_trades_are_buffered_until_drained():
    feed = live_feed()
    feed.handle_message(json.dumps({"event_type": "last_trade_price", "asset_id": "yes", "price": "0.5", "size": "12", "side": "sell"}))
    feed.handle_message("PONG")
    feed.handle_message("not json")
    trades = feed.drain_trades()
    assert [(t.token_id, t.price, t.size, t.side) for t in trades] == [("yes", 0.5, 12.0, "SELL")]
    assert feed.drain_trades() == []


def test_books_are_withheld_when_the_feed_goes_quiet_or_disconnects():
    feed = live_feed()
    feed.handle_message(json.dumps(book_event("yes", [(0.45, 10)], [(0.50, 5)])), now=100.0)
    assert feed.book("yes", now=129.0) is not None
    assert feed.book("yes", now=131.0) is None  # nothing heard for > 30 s
    feed.handle_message("PONG", now=131.0)
    assert feed.book("yes", now=131.0) is not None
    feed.connected = False
    assert feed.book("yes", now=131.0) is None


class FakeSocket:
    """Scripted server: replies to recv() from a queue, records what was sent."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = threading.Event()

    def send(self, data):
        self.sent.append(data)

    def settimeout(self, seconds):
        pass

    def recv(self):
        if self.closed.is_set():
            raise ConnectionError("closed")
        if self.messages:
            return self.messages.pop(0)
        time.sleep(0.01)
        raise type("WebSocketTimeoutException", (Exception,), {})()

    def close(self):
        self.closed.set()


def wait_for(condition, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_connection_loop_subscribes_streams_and_resubscribes_on_change():
    sockets = []

    def connect(url, timeout):
        sock = FakeSocket([json.dumps(book_event("yes", [(0.45, 10)], [(0.50, 5)]))])
        sockets.append(sock)
        return sock

    feed = MarketFeed(connect=connect, ping_interval=0.05)
    feed.set_assets(["yes", "no"])
    feed.start()
    try:
        assert wait_for(lambda: feed.book("yes") is not None)
        first = json.loads(sockets[0].sent[0])
        assert first == {"type": "market", "assets_ids": ["no", "yes"]}
        assert wait_for(lambda: "PING" in sockets[0].sent)

        feed.set_assets(["yes", "other"])  # a new set forces a fresh subscription
        assert wait_for(lambda: len(sockets) == 2 and sockets[1].sent)
        assert json.loads(sockets[1].sent[0])["assets_ids"] == ["other", "yes"]
        assert sockets[0].closed.is_set()
    finally:
        feed.stop()
    assert not feed.connected


def test_connection_failures_back_off_and_recover():
    attempts = []

    def connect(url, timeout):
        attempts.append(time.time())
        if len(attempts) == 1:
            raise OSError("network down")
        return FakeSocket([json.dumps(book_event("yes", [(0.45, 10)], [(0.50, 5)]))])

    feed = MarketFeed(connect=connect)
    feed.set_assets(["yes"])
    feed.start()
    try:
        assert wait_for(lambda: feed.book("yes") is not None, timeout=5.0)
        assert len(attempts) == 2
    finally:
        feed.stop()
