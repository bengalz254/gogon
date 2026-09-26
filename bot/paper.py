"""Paper-trading fill simulation.

Taker orders (fill-or-kill) fill only if the latest book holds the whole size
at or better than the limit price — if the book moved since the signal was
generated, the paper order fails just as a live one would.

Resting (maker) orders use a queue model. When an order is placed at a price
that already has S shares resting, those S shares are ahead of it. Trades at
that price eat the queue first and only the excess fills our order. A trade
at a better price than ours (a "trade-through"), or the other side of the
book moving onto or through our price, fills it outright. Cancellations at
our level are ignored — they may have been behind us — which keeps the
estimate on the pessimistic side. Only trades on the order's own token are
counted, so orders on the complementary token (buying NO instead of selling
YES) fill somewhat less often than they would live.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from bot.orderbook import OrderBook


def fok_fillable(book: OrderBook, side: str, limit_price: float, size: float) -> bool:
    """Whether `size` shares could be bought (asks <= limit) or sold (bids >= limit) now."""
    levels = book.asks_up_to(limit_price) if side == "BUY" else book.bids_down_to(limit_price)
    return sum(s for _, s in levels) + 1e-9 >= size


def would_cross(book: OrderBook | None, side: str, price: float) -> bool:
    """Whether a new order at `price` would trade immediately (a post-only order is rejected)."""
    if book is None:
        return False
    if side == "BUY":
        return book.best_ask is not None and price >= book.best_ask - 1e-12
    return book.best_bid is not None and price <= book.best_bid + 1e-12


@dataclass
class RestingOrder:
    order_id: str
    market_id: str
    token_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size: float
    filled: float = 0.0
    queue_ahead: float = 0.0
    placed_at: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)


@dataclass
class Fill:
    order_id: str
    market_id: str
    token_id: str
    side: str
    price: float
    size: float


class PaperMatcher:
    """Simulated resting orders for paper-mode market making."""

    def __init__(self):
        self.orders: dict[str, RestingOrder] = {}
        self._next_id = 0

    def place(
        self,
        market_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        book: OrderBook | None,
        now: float | None = None,
    ) -> RestingOrder | None:
        """Rest a post-only order; None if it would have crossed the book."""
        if would_cross(book, side, price):
            return None
        self._next_id += 1
        order = RestingOrder(
            order_id=f"paper-{self._next_id}",
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            queue_ahead=book.size_at(side, price) if book is not None else 0.0,
            placed_at=time.time() if now is None else now,
        )
        self.orders[order.order_id] = order
        return order

    def cancel(self, order_id: str) -> RestingOrder | None:
        return self.orders.pop(order_id, None)

    def on_trade(self, token_id: str, price: float, size: float) -> list[Fill]:
        """Apply a public trade of `size` shares at `price` on `token_id`."""
        fills = []
        for order in list(self.orders.values()):
            if order.token_id != token_id or order.remaining <= 0 or size <= 0:
                continue
            better = price < order.price - 1e-12 if order.side == "BUY" else price > order.price + 1e-12
            at_our_price = abs(price - order.price) <= 1e-12
            if better:
                qty = min(order.remaining, size)
            elif at_our_price:
                eaten = min(order.queue_ahead, size)
                order.queue_ahead -= eaten
                qty = min(order.remaining, size - eaten)
            else:
                continue
            if qty > 1e-12:
                fills.append(self._fill(order, qty))
        return fills

    def on_book(self, book: OrderBook) -> list[Fill]:
        """Fill orders the book has moved onto or through (the other side now
        sits at or beyond our price, which can't happen while we rest there)."""
        fills = []
        for order in list(self.orders.values()):
            if order.token_id != book.token_id or order.remaining <= 0:
                continue
            if order.side == "BUY":
                crossed = book.best_ask is not None and book.best_ask <= order.price + 1e-12
            else:
                crossed = book.best_bid is not None and book.best_bid >= order.price - 1e-12
            if crossed:
                fills.append(self._fill(order, order.remaining))
        return fills

    def _fill(self, order: RestingOrder, qty: float) -> Fill:
        order.filled += qty
        if order.remaining <= 1e-9:
            self.orders.pop(order.order_id, None)
        return Fill(order.order_id, order.market_id, order.token_id, order.side, order.price, qty)
