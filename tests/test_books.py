from bot.books import BookProvider
from bot.orderbook import OrderBook


class FakeClient:
    def __init__(self, batch_fails=False):
        self.batch_calls = []
        self.single_calls = []
        self.batch_fails = batch_fails

    def _raw(self, token):
        return {"asset_id": token, "bids": [{"price": "0.45", "size": "10"}], "asks": [{"price": "0.50", "size": "5"}]}

    def get_order_books(self, params):
        self.batch_calls.append([p["token_id"] for p in params])
        if self.batch_fails:
            raise RuntimeError("503")
        return [self._raw(p["token_id"]) for p in params]

    def get_order_book(self, token_id):
        self.single_calls.append(token_id)
        if token_id == "broken":
            raise RuntimeError("404")
        return self._raw(token_id)


class FakeFeed:
    def __init__(self, books):
        self.books = books

    def book(self, token_id):
        return self.books.get(token_id)


def test_prefetch_batches_and_reuses_snapshots_within_a_cycle():
    client = FakeClient()
    provider = BookProvider(client, batch_size=2)
    provider.prefetch(["a", "b", "c", "a"])
    assert client.batch_calls == [["a", "b"], ["c"]]
    assert provider.top("b").best_ask == 0.50
    assert client.single_calls == []  # served from the batch
    provider.new_cycle()
    provider.top("b")
    assert client.single_calls == ["b"]  # a new cycle fetches fresh


def test_live_websocket_books_win_and_are_not_fetched():
    client = FakeClient()
    live = OrderBook("a", bids={0.47: 1.0}, asks={0.48: 1.0})
    provider = BookProvider(client, FakeFeed({"a": live}))
    provider.prefetch(["a", "b"])
    assert client.batch_calls == [["b"]]
    assert provider.top("a").best_bid == 0.47


def test_failures_fall_back_and_are_not_retried_in_the_same_cycle():
    client = FakeClient(batch_fails=True)
    provider = BookProvider(client)
    provider.prefetch(["a"])
    assert provider.top("a").best_bid == 0.45  # single-request fallback
    assert provider.top("broken").best_bid is None
    provider.top("broken")
    assert client.single_calls.count("broken") == 1
