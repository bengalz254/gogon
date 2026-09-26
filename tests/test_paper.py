from bot.orderbook import OrderBook
from bot.paper import PaperMatcher, fok_fillable, would_cross


def book(bids=None, asks=None):
    return OrderBook("tok", bids=dict(bids or {}), asks=dict(asks or {}))


def test_fok_fillable_walks_depth_up_to_the_limit():
    b = book(bids={0.45: 5, 0.44: 5}, asks={0.50: 5, 0.51: 5, 0.60: 100})
    assert fok_fillable(b, "BUY", 0.51, 10)
    assert not fok_fillable(b, "BUY", 0.50, 10)
    assert fok_fillable(b, "SELL", 0.44, 10)
    assert not fok_fillable(b, "SELL", 0.45, 6)


def test_post_only_order_that_would_cross_is_rejected():
    b = book(bids={0.45: 5}, asks={0.50: 5})
    assert would_cross(b, "BUY", 0.50) and would_cross(b, "SELL", 0.45)
    matcher = PaperMatcher()
    assert matcher.place("m", "tok", "BUY", 0.50, 10, b) is None
    assert matcher.place("m", "tok", "BUY", 0.46, 10, b) is not None


def test_trades_at_our_price_eat_the_queue_ahead_first():
    matcher = PaperMatcher()
    order = matcher.place("m", "tok", "BUY", 0.45, 10, book(bids={0.45: 30}, asks={0.50: 5}))
    assert order.queue_ahead == 30
    assert matcher.on_trade("tok", 0.45, 25) == []  # 5 still ahead of us
    fills = matcher.on_trade("tok", 0.45, 8)  # 5 more ahead, then 3 for us
    assert [(f.size, f.price) for f in fills] == [(3, 0.45)]
    assert matcher.on_trade("tok", 0.46, 50) == []  # a higher price doesn't reach our bid


def test_trade_through_fills_immediately_at_our_price():
    matcher = PaperMatcher()
    matcher.place("m", "tok", "SELL", 0.55, 10, book(bids={0.45: 5}, asks={0.55: 100}))
    fills = matcher.on_trade("tok", 0.57, 4)  # bought above our ask: we'd have been hit first
    assert [(f.side, f.price, f.size) for f in fills] == [("SELL", 0.55, 4)]


def test_book_moving_through_our_price_fills_the_rest():
    matcher = PaperMatcher()
    order = matcher.place("m", "tok", "BUY", 0.45, 10, book(bids={0.45: 5}, asks={0.50: 5}))
    fills = matcher.on_book(book(bids={0.40: 5}, asks={0.44: 5}))
    assert [f.size for f in fills] == [10]
    assert order.order_id not in matcher.orders  # fully filled orders are removed


def test_cancelled_orders_never_fill():
    matcher = PaperMatcher()
    order = matcher.place("m", "tok", "BUY", 0.45, 10, book(asks={0.50: 5}))
    matcher.cancel(order.order_id)
    assert matcher.on_trade("tok", 0.40, 100) == []
