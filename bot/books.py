"""One place to get order books from.

The WebSocket feed's book when it has a live one; otherwise a REST snapshot,
fetched in batches (one `/books` request per `batch_size` tokens instead of a
request per token) and reused for the rest of the scan cycle.
"""
from __future__ import annotations

import logging
from typing import Iterable

from bot.orderbook import BookLevel, OrderBook
from bot.ws_market import MarketFeed

logger = logging.getLogger("polybot.books")

EMPTY_LEVEL = BookLevel(None, None, 0.0, 0.0)


class BookProvider:
    def __init__(self, client, feed: MarketFeed | None = None, batch_size: int = 100):
        self.client = client
        self.feed = feed
        self.batch_size = max(1, batch_size)
        self._rest: dict[str, OrderBook | None] = {}

    def new_cycle(self) -> None:
        """Forget REST snapshots so the next reads fetch fresh ones."""
        self._rest.clear()

    def prefetch(self, token_ids: Iterable[str]) -> None:
        """Fetch REST snapshots for tokens the feed can't serve, in batches."""
        wanted = [
            t for t in dict.fromkeys(token_ids) if t and t not in self._rest and self._live_book(t) is None
        ]
        for start in range(0, len(wanted), self.batch_size):
            batch = wanted[start : start + self.batch_size]
            try:
                raw_books = self.client.get_order_books([{"token_id": t} for t in batch])
            except Exception as exc:
                logger.warning("Batch book fetch failed (%s); falling back to single requests", type(exc).__name__)
                continue
            for raw in raw_books if isinstance(raw_books, list) else []:
                book = OrderBook.from_snapshot(raw)
                if book.token_id in batch:
                    self._rest[book.token_id] = book

    def book(self, token_id: str) -> OrderBook | None:
        live = self._live_book(token_id)
        if live is not None:
            return live
        if token_id not in self._rest:
            try:
                self._rest[token_id] = OrderBook.from_snapshot(self.client.get_order_book(token_id), token_id=token_id)
            except Exception:
                logger.exception("Failed to fetch order book for token %s", token_id)
                self._rest[token_id] = None  # don't retry within this cycle
        return self._rest[token_id]

    def top(self, token_id: str) -> BookLevel:
        book = self.book(token_id)
        return book.top() if book is not None else EMPTY_LEVEL

    def _live_book(self, token_id: str) -> OrderBook | None:
        return self.feed.book(token_id) if self.feed is not None else None
