"""Orders: request/update types, the engine's order tracker, and the paper
exchange used in paper mode, backtests and the simulator.

Paper fills are deliberately conservative:
- Taker (FAK/FOK) orders walk the *real* ask ladder up to the limit price
  and pay the taker fee. Liquidity we "took" is remembered until the feed
  updates that level, so paper mode can't eat the same shares twice.
- Maker (GTC, post-only) bids join the back of the queue at their price.
  They fill only when the market trades *through* them, trades at their
  price exceed the size queued ahead, the ask side comes down to them, or a
  complement bid crosses (Up bid p + Down bid >= 1 would mint a pair).
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

from bot.updown.book import OrderBook
from bot.updown.fees import FeeModel

_ids = itertools.count(1)


def new_client_id(prefix: str = "ud") -> str:
    return f"{prefix}-{next(_ids)}"


@dataclass
class OrderRequest:
    client_id: str
    window_id: str
    strategy: str
    token_id: str
    outcome: str
    price: float  # limit price
    shares: float
    tif: str  # FAK | FOK | GTC
    post_only: bool = False
    quote_key: str | None = None
    tick_size: float = 0.01
    neg_risk: bool = False
    reason: str = ""
    created_ts: float = 0.0
    side: str = "BUY"
    meta: dict = field(default_factory=dict)

    @property
    def is_maker(self) -> bool:
        return self.tif == "GTC"


@dataclass
class Fill:
    shares: float
    price: float
    fee: float  # USDC
    ts: float
    is_maker: bool


@dataclass
class OrderUpdate:
    client_id: str
    status: str  # open | partial | filled | cancelled | rejected
    fills: list = field(default_factory=list)  # new fills since the last update
    exchange_id: str | None = None
    final: bool = False
    message: str = ""


@dataclass
class PlaceOrder:
    req: OrderRequest


@dataclass
class CancelOrder:
    client_id: str
    reason: str = ""


@dataclass
class OrderState:
    req: OrderRequest
    status: str = "pending"
    exchange_id: str | None = None
    filled: float = 0.0
    cost: float = 0.0  # incl. fees
    fees: float = 0.0
    final: bool = False
    cancel_requested: bool = False
    updated_ts: float = 0.0
    message: str = ""

    @property
    def remaining(self) -> float:
        return 0.0 if self.final else max(0.0, self.req.shares - self.filled)

    @property
    def reserved_usd(self) -> float:
        return self.remaining * self.req.price


class OrderTracker:
    def __init__(self):
        self.orders: dict[str, OrderState] = {}

    def add(self, req: OrderRequest) -> OrderState:
        st = OrderState(req=req, updated_ts=req.created_ts)
        self.orders[req.client_id] = st
        return st

    def get(self, client_id: str) -> OrderState | None:
        return self.orders.get(client_id)

    def apply(self, update: OrderUpdate, now: float) -> list[Fill]:
        st = self.orders.get(update.client_id)
        if st is None:
            return []
        new_fills = []
        for f in update.fills:
            room = st.req.shares - st.filled
            shares = min(f.shares, room)
            if shares <= 1e-9:
                continue
            fill = Fill(shares, f.price, f.fee * shares / f.shares if f.shares else 0.0, f.ts, f.is_maker)
            st.filled += shares
            st.cost += shares * fill.price + fill.fee
            st.fees += fill.fee
            new_fills.append(fill)
        if update.exchange_id:
            st.exchange_id = update.exchange_id
        st.status = update.status
        st.message = update.message or st.message
        st.final = st.final or update.final or st.filled >= st.req.shares - 1e-9
        st.updated_ts = now
        return new_fills

    def live(self, window_id: str | None = None, strategy: str | None = None):
        for st in self.orders.values():
            if st.final:
                continue
            if window_id is not None and st.req.window_id != window_id:
                continue
            if strategy is not None and st.req.strategy != strategy:
                continue
            yield st

    def quote(self, window_id: str, strategy: str, key: str) -> OrderState | None:
        for st in self.live(window_id, strategy):
            if st.req.quote_key == key and not st.cancel_requested:
                return st
        return None

    def reserved(self, window_id: str | None = None, strategy: str | None = None, exclude: str | None = None) -> float:
        return sum(
            st.reserved_usd for st in self.live(window_id, strategy) if st.req.client_id != exclude
        )

    def has_inflight_take(self, window_id: str, strategy: str) -> bool:
        return any(st.req.quote_key is None for st in self.live(window_id, strategy))

    def prune(self, now: float, keep_s: float = 900.0) -> None:
        stale = [cid for cid, st in self.orders.items() if st.final and now - st.updated_ts > keep_s]
        for cid in stale:
            del self.orders[cid]


# --------------------------------------------------------------------------
# Paper exchange
# --------------------------------------------------------------------------
@dataclass
class _Resting:
    req: OrderRequest
    remaining: float
    queue_ahead: float


class PaperExchange:
    """Simulated matching against the real (or simulated) public books.

    `books` is the engine's token -> OrderBook map (shared, read-only here);
    `complement` maps each token to the other outcome's token.
    """

    def __init__(self, books: dict, fees: FeeModel, complement: dict):
        self.books = books
        self.fees = fees
        self.complement = complement
        self.resting: dict[str, _Resting] = {}
        self._consumed: dict[str, dict[float, float]] = {}

    # -- order entry -------------------------------------------------------------
    def submit(self, req: OrderRequest, now: float) -> OrderUpdate:
        book = self.books.get(req.token_id)
        if book is None or not book.has_snapshot:
            return OrderUpdate(req.client_id, "rejected", final=True, message="no book")
        if req.tif in ("FAK", "FOK"):
            return self._take(req, book, now)
        if self._crosses(req.token_id, req.price):
            if req.post_only:
                return OrderUpdate(req.client_id, "rejected", final=True, message="post-only would cross")
            return self._take(req, book, now)
        queue = book.size_at("BUY", req.price)
        self.resting[req.client_id] = _Resting(req, req.shares, queue)
        return OrderUpdate(req.client_id, "open", exchange_id=f"paper-{req.client_id}")

    def cancel(self, client_id: str, now: float) -> OrderUpdate:
        r = self.resting.pop(client_id, None)
        return OrderUpdate(client_id, "cancelled", final=True, message="" if r else "not resting")

    def _take(self, req: OrderRequest, book: OrderBook, now: float) -> OrderUpdate:
        consumed = self._consumed.setdefault(req.token_id, {})
        plan = []
        want = req.shares
        for price, size in book.asks_up_to(req.price):
            avail = size - consumed.get(price, 0.0)
            if avail <= 1e-9:
                continue
            take = min(avail, want)
            plan.append((price, take))
            want -= take
            if want <= 1e-9:
                break
        got = sum(s for _, s in plan)
        if got <= 1e-9 or (req.tif == "FOK" and want > 1e-6):
            return OrderUpdate(req.client_id, "cancelled", final=True, message="no liquidity at limit")
        fills = []
        for price, shares in plan:
            consumed[price] = consumed.get(price, 0.0) + shares
            fills.append(Fill(shares, price, shares * self.fees.taker_per_share(price), now, False))
        status = "filled" if want <= 1e-6 else "partial"
        return OrderUpdate(req.client_id, status, fills=fills, exchange_id=f"paper-{req.client_id}", final=True)

    def _crosses(self, token_id: str, price: float) -> bool:
        book = self.books.get(token_id)
        if book is not None and book.best_ask is not None and book.best_ask <= price + 1e-9:
            return True
        comp = self.books.get(self.complement.get(token_id, ""))
        return comp is not None and comp.best_bid is not None and comp.best_bid >= 1.0 - price - 1e-9

    # -- market events -----------------------------------------------------------------
    def on_trade(self, token_id: str, price: float, size: float, now: float) -> list[OrderUpdate]:
        out = []
        for cid, r in list(self.resting.items()):
            if r.req.token_id != token_id:
                continue
            fill_qty = 0.0
            if price < r.req.price - 1e-9:
                fill_qty = r.remaining  # traded through our level
            elif abs(price - r.req.price) <= 1e-9:
                r.queue_ahead -= size
                if r.queue_ahead < 0:
                    fill_qty = min(r.remaining, -r.queue_ahead)
                    r.queue_ahead = 0.0
            if fill_qty > 1e-9:
                out.append(self._maker_fill(cid, r, fill_qty, now))
        return out

    def on_book(self, token_id: str, now: float, snapshot: bool = False, level_price: float | None = None,
                level_side: str | None = None) -> list[OrderUpdate]:
        if snapshot:
            self._consumed.pop(token_id, None)
        elif level_price is not None and level_side == "SELL":
            self._consumed.get(token_id, {}).pop(round(level_price, 6), None)
        out = []
        for cid, r in list(self.resting.items()):
            tok = r.req.token_id
            if tok != token_id and self.complement.get(tok) != token_id:
                continue
            book = self.books.get(tok)
            if book is not None and level_side == "BUY" and tok == token_id and level_price is not None \
                    and abs(level_price - r.req.price) <= 1e-9:
                # size ahead of us can only shrink (cancels ahead of us move us up)
                r.queue_ahead = min(r.queue_ahead, book.size_at("BUY", r.req.price))
            if self._crosses(tok, r.req.price):
                out.append(self._maker_fill(cid, r, r.remaining, now))
        return out

    def _maker_fill(self, cid: str, r: _Resting, qty: float, now: float) -> OrderUpdate:
        qty = min(qty, r.remaining)
        r.remaining -= qty
        fee = qty * self.fees.maker_per_share(r.req.price)
        done = r.remaining <= 1e-9
        if done:
            self.resting.pop(cid, None)
        return OrderUpdate(
            cid, "filled" if done else "partial",
            fills=[Fill(qty, r.req.price, fee, now, True)], final=done,
        )


def floor_shares(x: float) -> float:
    return math.floor(x * 100.0) / 100.0


class PaperBroker:
    """Executes engine actions against a PaperExchange and turns market
    events into maker fills. Used by the paper runner, backtests and the
    simulator."""

    def __init__(self, engine):
        self.exchange = PaperExchange(engine.books, engine.fees, engine.complement)

    def execute(self, actions: list, now: float) -> list[OrderUpdate]:
        updates = []
        for a in actions:
            if isinstance(a, PlaceOrder):
                updates.append(self.exchange.submit(a.req, now))
            elif isinstance(a, CancelOrder):
                updates.append(self.exchange.cancel(a.client_id, now))
        return updates

    def on_event(self, ev, now: float) -> list[OrderUpdate]:
        k = ev.kind
        if k == "trade":
            return self.exchange.on_trade(ev.token_id, ev.price, ev.size, now)
        if k == "book":
            return self.exchange.on_book(ev.token_id, now, snapshot=True)
        if k == "level":
            return self.exchange.on_book(ev.token_id, now, level_price=ev.price, level_side=ev.side)
        return []
