from bot.market_data import best_levels
from bot.orderbook import OrderBook, parse_levels


class Level:  # V1 SDK style: objects with .price/.size strings
    def __init__(self, price, size):
        self.price, self.size = price, size


class V1Book:
    def __init__(self, bids, asks):
        self.bids, self.asks = bids, asks


def test_parse_levels_accepts_dicts_objects_and_pairs():
    assert parse_levels([{"price": "0.5", "size": "10"}, Level("0.4", "5"), ["0.3", "2"], {"price": "x"}]) == [
        (0.5, 10.0),
        (0.4, 5.0),
        (0.3, 2.0),
    ]
    assert parse_levels([{"price": "0.5", "size": "0"}]) == []  # empty levels dropped
    assert parse_levels(None) == []


def test_snapshot_from_v2_dict_picks_best_levels_regardless_of_order():
    raw = {
        "asset_id": "tok",
        "bids": [{"price": "0.40", "size": "5"}, {"price": "0.45", "size": "7"}],
        "asks": [{"price": "0.55", "size": "3"}, {"price": "0.50", "size": "9"}],
        "tick_size": "0.01",
        "min_order_size": "5",
    }
    book = OrderBook.from_snapshot(raw)
    assert book.token_id == "tok"
    assert (book.best_bid, book.best_ask, book.mid) == (0.45, 0.50, 0.475)
    assert (book.tick_size, book.min_order_size) == (0.01, 5.0)
    top = book.top()
    assert (top.best_bid_size, top.best_ask_size) == (7.0, 9.0)


def test_best_levels_works_for_v1_objects_and_v2_dicts():
    v1 = best_levels(V1Book([Level("0.45", "7")], [Level("0.50", "9")]))
    v2 = best_levels({"bids": [{"price": "0.45", "size": "7"}], "asks": [{"price": "0.50", "size": "9"}]})
    assert v1 == v2
    empty = best_levels({"bids": [], "asks": []})
    assert (empty.best_bid, empty.best_ask, empty.best_bid_size) == (None, None, 0.0)


def test_deltas_update_and_remove_levels():
    book = OrderBook("tok", bids={0.45: 7.0}, asks={0.50: 9.0})
    book.apply_change("BUY", 0.46, 3.0, now=10.0)
    book.apply_change("SELL", 0.50, 0.0, now=11.0)  # level emptied
    book.apply_change("SELL", 0.52, 4.0, now=12.0)
    assert (book.best_bid, book.best_ask, book.updated_at) == (0.46, 0.52, 12.0)
    assert book.size_at("BUY", 0.45) == 7.0


def test_depth_queries():
    book = OrderBook("tok", bids={0.45: 7.0, 0.44: 2.0, 0.40: 1.0}, asks={0.50: 9.0, 0.51: 1.0, 0.60: 5.0})
    assert book.asks_up_to(0.51) == [(0.50, 9.0), (0.51, 1.0)]
    assert book.bids_down_to(0.44) == [(0.45, 7.0), (0.44, 2.0)]
    copy = book.copy()
    copy.apply_change("BUY", 0.45, 0.0)
    assert book.best_bid == 0.45  # the copy is independent
