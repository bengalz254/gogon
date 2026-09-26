"""Order books with full depth.

One OrderBook per token, built from a REST snapshot (`/book`, `/books`) or a
WebSocket `book` event and kept current with `price_change` deltas. Prices
and sizes arrive as strings, as dicts ({"price": "0.5", "size": "10"}) or
as objects with .price/.size (older SDKs) — all are accepted.

Strategies mostly need the top of the book (BookLevel); market making and
the paper-fill simulator need the depth.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class BookLevel:
    """Best bid/ask summary for one token."""

    best_bid: float | None
    best_ask: float | None
    best_bid_size: float
    best_ask_size: float


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_levels(entries) -> list[tuple[float, float]]:
    """(price, size) pairs from a list of level dicts, objects or [price, size] pairs."""
    levels = []
    for entry in entries or []:
        if isinstance(entry, dict):
            price, size = entry.get("price"), entry.get("size")
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            price, size = entry[0], entry[1]
        else:
            price, size = getattr(entry, "price", None), getattr(entry, "size", None)
        price, size = _to_float(price), _to_float(size)
        if price is not None and size is not None and size > 0:
            levels.append((price, size))
    return levels


def _field(raw, name):
    return raw.get(name) if isinstance(raw, dict) else getattr(raw, name, None)


@dataclass
class OrderBook:
    token_id: str
    bids: dict[float, float] = field(default_factory=dict)  # price -> size
    asks: dict[float, float] = field(default_factory=dict)
    tick_size: float | None = None
    min_order_size: float | None = None
    updated_at: float = 0.0  # time.time() of the last update

    @classmethod
    def from_snapshot(cls, raw, token_id: str | None = None, now: float | None = None) -> "OrderBook":
        """Build a book from a REST/WebSocket snapshot (dict or SDK object)."""
        book = cls(token_id=str(token_id or _field(raw, "asset_id") or ""))
        book.apply_snapshot(
            _field(raw, "bids") or _field(raw, "buys"),
            _field(raw, "asks") or _field(raw, "sells"),
            now=now,
        )
        book.tick_size = _to_float(_field(raw, "tick_size"))
        book.min_order_size = _to_float(_field(raw, "min_order_size"))
        return book

    def apply_snapshot(self, bids, asks, now: float | None = None) -> None:
        self.bids = dict(parse_levels(bids))
        self.asks = dict(parse_levels(asks))
        self.updated_at = time.time() if now is None else now

    def apply_change(self, side: str, price: float, size: float, now: float | None = None) -> None:
        """Set the aggregate size at one price level; size 0 removes the level.

        `side` is the side of the resting orders: BUY updates bids, SELL asks.
        """
        levels = self.bids if str(side).upper() == "BUY" else self.asks
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self.updated_at = time.time() if now is None else now

    # -- queries -----------------------------------------------------------
    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def mid(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    def top(self) -> BookLevel:
        bid, ask = self.best_bid, self.best_ask
        return BookLevel(
            best_bid=bid,
            best_ask=ask,
            best_bid_size=self.bids[bid] if bid is not None else 0.0,
            best_ask_size=self.asks[ask] if ask is not None else 0.0,
        )

    def asks_up_to(self, limit_price: float) -> list[tuple[float, float]]:
        """Ask levels priced at or below `limit_price`, cheapest first."""
        return sorted((p, s) for p, s in self.asks.items() if p <= limit_price + 1e-12)

    def bids_down_to(self, limit_price: float) -> list[tuple[float, float]]:
        """Bid levels priced at or above `limit_price`, highest first."""
        return sorted(((p, s) for p, s in self.bids.items() if p >= limit_price - 1e-12), reverse=True)

    def size_at(self, side: str, price: float) -> float:
        levels = self.bids if str(side).upper() == "BUY" else self.asks
        return levels.get(price, 0.0)

    def age(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) - self.updated_at

    def copy(self) -> "OrderBook":
        return OrderBook(
            token_id=self.token_id,
            bids=dict(self.bids),
            asks=dict(self.asks),
            tick_size=self.tick_size,
            min_order_size=self.min_order_size,
            updated_at=self.updated_at,
        )
