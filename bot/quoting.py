"""Keeps the market maker's resting orders in line with its desired quotes.

Each refresh the strategy says what it wants quoted; this module cancels
orders that are no longer wanted (or drifted too far), places the missing
ones, and turns fills into positions, journal rows and saved state.

Paper mode rests orders in a simulated book (bot/paper.py) and fills them
from the market's real trades and book moves. Live mode places post-only
GTD orders that expire on their own shortly after the bot stops refreshing
them, reads fills from the account's open orders, and confirms the final
fill of every order that disappears (filled, cancelled or expired).

Budget: resting BUY orders count against the market's exposure cap as if
they had filled, and SELL orders are only placed for shares actually held.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from bot.config import MarketMakerConfig
from bot.fees import FeeModel
from bot.journal import TradeJournal
from bot.market_data import MarketInfo
from bot.orderbook import OrderBook
from bot.paper import PaperMatcher
from bot.risk import RiskManager
from bot.strategies.base import Signal
from bot.strategies.market_maker import Quote

logger = logging.getLogger("polybot.quoting")

_EPS = 1e-9
# Give up confirming a vanished order's final fill after this many failed lookups.
MAX_FINAL_CHECKS = 3


@dataclass
class WorkingOrder:
    order_id: str
    market_id: str
    token_id: str
    outcome: str
    side: str
    price: float
    size: float
    filled: float = 0.0
    placed_at: float = 0.0
    final_checks: int = 0

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class QuoteManager:
    def __init__(
        self,
        client,
        risk: RiskManager,
        journal: TradeJournal,
        fees: FeeModel,
        cfg: MarketMakerConfig,
        live: bool,
        store=None,
        matcher: PaperMatcher | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.client = client
        self.risk = risk
        self.journal = journal
        self.fees = fees
        self.cfg = cfg
        self.live = live
        self.store = store
        self.matcher = matcher if matcher is not None else PaperMatcher()
        self.clock = clock
        self.orders: dict[str, WorkingOrder] = {}
        # Live orders cancelled or gone from the book whose final fill is unconfirmed.
        self._closing: dict[str, WorkingOrder] = {}
        self._markets: dict[str, MarketInfo] = {}
        self._stats_day = datetime.now(timezone.utc).date()
        self.fills_today = 0
        self.volume_today = 0.0

    @property
    def mode(self) -> str:
        return "live" if self.live else "paper"

    # -- desired state -------------------------------------------------------------
    def sync(self, market: MarketInfo, quotes: list[Quote], get_book: Callable[[str], OrderBook | None]) -> None:
        """Make the market's resting orders match `quotes`.

        An order is kept when a quote wants the same token and side, its price
        is within requote_ticks, at least half its size is left, and (live) it
        isn't about to expire. Everything else is cancelled and re-placed.
        """
        market_id = market.condition_id
        self._markets[market_id] = market
        book = get_book(market.tokens[0].token_id) if market.tokens else None
        tick = (book.tick_size if book is not None else None) or market.tick_size or 0.01
        now = self.clock()
        current = [o for o in self.orders.values() if o.market_id == market_id]
        keep: set[str] = set()
        to_place: list[Quote] = []
        for quote in quotes:
            match = next(
                (o for o in current if o.token_id == quote.token_id and o.side == quote.side and o.order_id not in keep),
                None,
            )
            if match is not None and self._still_good(match, quote, tick, now):
                keep.add(match.order_id)
            else:
                to_place.append(quote)
        stale = [o for o in current if o.order_id not in keep]
        if stale:
            self._cancel(stale)
        for quote in to_place:
            self._place(market, quote, get_book)

    def cancel_market(self, market_id: str, reason: str) -> None:
        orders = [o for o in self.orders.values() if o.market_id == market_id]
        if orders:
            logger.info("Pulling %d quotes in market %s: %s", len(orders), market_id, reason)
            self._cancel(orders)

    def cancel_all(self, reason: str) -> None:
        """Pull every quote. Live, this cancels all of the account's open orders."""
        if not self.orders and not self.live:
            return
        logger.info("Pulling all quotes (%d): %s", len(self.orders), reason)
        if self.live:
            try:
                self.client.cancel_all()
            except Exception as exc:
                logger.warning("cancel_all failed (%s); orders will expire on their own", type(exc).__name__)
            self._closing.update(self.orders)
            self.orders.clear()
        else:
            self._cancel(list(self.orders.values()))

    def _still_good(self, order: WorkingOrder, quote: Quote, tick: float, now: float) -> bool:
        if abs(order.price - quote.price) > self.cfg.requote_ticks * tick - _EPS:
            return False
        if order.remaining < 0.5 * quote.size:
            return False
        return not self.live or now - order.placed_at < self.cfg.order_ttl_seconds

    # -- placing and cancelling -----------------------------------------------------
    def _place(self, market: MarketInfo, quote: Quote, get_book: Callable[[str], OrderBook | None]) -> None:
        market_id = market.condition_id
        if quote.side == "BUY":
            pending = sum(o.remaining * o.price for o in self.orders.values() if o.market_id == market_id and o.side == "BUY")
            cost = quote.size * quote.price + self.fees.schedule(market).maker_fee(quote.size, quote.price)
            allowed, reason = self.risk.can_open(market_id, pending + cost)
            if not allowed:
                logger.debug("Not quoting BUY %s in %s: %s", quote.token_id, market_id, reason)
                return
        else:
            position = self.risk.positions.get(quote.token_id)
            held = position.size if position is not None else 0.0
            reserved = sum(o.remaining for o in self.orders.values() if o.token_id == quote.token_id and o.side == "SELL")
            if held - reserved < quote.size - _EPS:
                return

        outcome = next((t.outcome for t in market.tokens if t.token_id == quote.token_id), "")
        if self.live:
            order_id = self._place_live(quote)
        else:
            resting = self.matcher.place(
                market_id, quote.token_id, quote.side, quote.price, quote.size, get_book(quote.token_id), now=self.clock()
            )
            order_id = resting.order_id if resting is not None else None
        if order_id is None:
            return
        self.orders[order_id] = WorkingOrder(
            order_id=order_id,
            market_id=market_id,
            token_id=quote.token_id,
            outcome=outcome,
            side=quote.side,
            price=quote.price,
            size=quote.size,
            placed_at=self.clock(),
        )

    def _place_live(self, quote: Quote) -> str | None:
        from py_clob_client_v2 import OrderArgs, OrderType

        # GTD: the exchange requires the expiry to be at least 60 s ahead.
        expiration = int(self.clock()) + 60 + int(self.cfg.order_ttl_seconds)
        try:
            order = self.client.create_order(
                OrderArgs(
                    token_id=quote.token_id,
                    price=quote.price,
                    size=quote.size,
                    side=quote.side,
                    expiration=expiration,
                )
            )
            response = self.client.post_order(order, OrderType.GTD, post_only=True)
        except Exception as exc:
            logger.warning("Placing %s %s @ %.4f failed (%s)", quote.side, quote.token_id, quote.price, type(exc).__name__)
            return None
        if isinstance(response, dict) and response.get("success") is True and response.get("orderID") and not response.get("errorMsg"):
            return str(response["orderID"])
        logger.info("Quote %s %s @ %.4f rejected: %s", quote.side, quote.token_id, quote.price, response)
        return None

    def _cancel(self, orders: list[WorkingOrder]) -> None:
        if self.live:
            try:
                self.client.cancel_orders([o.order_id for o in orders])
            except Exception as exc:
                logger.warning("Cancelling %d orders failed (%s); they expire on their own", len(orders), type(exc).__name__)
            for order in orders:
                self._closing[order.order_id] = self.orders.pop(order.order_id, order)
        else:
            for order in orders:
                self.matcher.cancel(order.order_id)
                self.orders.pop(order.order_id, None)

    # -- fills ------------------------------------------------------------------
    def poll_fills(self, trades, get_book: Callable[[str], OrderBook | None]) -> int:
        """Collect fills since the last call; returns how many were recorded.

        Paper: `trades` (public trades from the WebSocket) and the latest
        books drive the simulated fills. Live: the account's open orders are
        read and every vanished order's final state is confirmed.
        """
        if self.live:
            return self._poll_live()
        fills = []
        for trade in trades:
            fills.extend(self.matcher.on_trade(trade.token_id, trade.price, trade.size))
        for token_id in {o.token_id for o in self.orders.values()}:
            book = get_book(token_id)
            if book is not None:
                fills.extend(self.matcher.on_book(book))
        for fill in fills:
            order = self.orders.get(fill.order_id)
            if order is None:
                continue
            order.filled += fill.size
            self._record_fill(order, fill.size)
            if order.remaining <= _EPS:
                self.orders.pop(order.order_id, None)
        return len(fills)

    def _poll_live(self) -> int:
        if not self.orders and not self._closing:
            return 0
        recorded = 0
        if self.orders:
            try:
                open_orders = self.client.get_open_orders()
            except Exception as exc:
                logger.warning("Reading open orders failed (%s)", type(exc).__name__)
                return 0
            by_id = {str(o.get("id")): o for o in open_orders or [] if isinstance(o, dict)}
            for order_id, order in list(self.orders.items()):
                seen = by_id.get(order_id)
                if seen is None:
                    self._closing[order_id] = self.orders.pop(order_id)  # filled, expired or cancelled
                else:
                    recorded += self._catch_up(order, _float(seen.get("size_matched")))
        for order_id, order in list(self._closing.items()):
            try:
                final = self.client.get_order(order_id)
            except Exception as exc:
                order.final_checks += 1
                if order.final_checks >= MAX_FINAL_CHECKS:
                    logger.warning(
                        "Couldn't confirm the final fill of order %s (%s); reconciliation will catch any gap",
                        order_id,
                        type(exc).__name__,
                    )
                    del self._closing[order_id]
                continue
            if isinstance(final, dict) and isinstance(final.get("order"), dict):
                final = final["order"]
            matched = _float(final.get("size_matched")) if isinstance(final, dict) else None
            recorded += self._catch_up(order, matched)
            del self._closing[order_id]
        return recorded

    def _catch_up(self, order: WorkingOrder, matched: float | None) -> int:
        if matched is None or matched <= order.filled + _EPS:
            return 0
        qty = min(matched, order.size) - order.filled
        order.filled += qty
        self._record_fill(order, qty)
        return 1

    def _record_fill(self, order: WorkingOrder, qty: float) -> None:
        market = self._markets.get(order.market_id)
        fee = self.fees.schedule(market).maker_fee(qty, order.price) if market is not None else 0.0
        notional = qty * order.price
        if order.side == "BUY":
            self.risk.record_open(
                order.market_id, order.token_id, order.outcome, qty, notional + fee, outcome_count=2
            )
        else:
            self.risk.record_close(order.token_id, qty, notional - fee)
        if self.store is not None:
            try:
                self.store.save(self.risk.snapshot())
            except Exception:
                logger.exception("Failed to persist risk state after a maker fill")
        try:
            self.journal.record(
                Signal(
                    strategy="market_maker",
                    market_id=order.market_id,
                    token_id=order.token_id,
                    outcome=order.outcome,
                    side=order.side,
                    limit_price=order.price,
                    size_shares=qty,
                    size_usd=notional,
                    reason="maker fill",
                    fee_usd=fee,
                    outcome_count=2,
                ),
                mode=self.mode,
                filled=True,
            )
        except Exception:
            logger.exception("Failed to write the trade journal for a maker fill")
        today = datetime.now(timezone.utc).date()
        if today != self._stats_day:
            self._stats_day, self.fills_today, self.volume_today = today, 0, 0.0
        self.fills_today += 1
        self.volume_today += notional
        logger.info(
            "[%s] maker fill: %s %.2f %s @ %.4f (market %s)",
            self.mode.upper(),
            order.side,
            qty,
            order.outcome,
            order.price,
            order.market_id,
        )
