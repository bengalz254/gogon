"""Market making on 5-minute Up/Down markets.

The idea: rest post-only BUY orders on both Up and Down, below the market's
fair price. One Up share plus one Down share always pays exactly $1, so a
pair bought for, say, 0.47 + 0.49 = 0.96 locks in 4c whatever happens.
Makers pay no fee on these markets, unlike takers.

The risk is adverse selection: our Up bid gets hit because Up just became
less likely, and the Down side never fills. The quoter limits that by
leaning toward completing pairs, refusing to overpay for a pair, pulling
quotes after sharp moves, and going quiet near the close.

Everything here is pure logic or order bookkeeping; the engine does the IO.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from updown.config import MakerConfig
from updown.strategy import DOWN, UP, Book, WindowPosition

logger = logging.getLogger("polybot.updown.maker")

OTHER = {UP: DOWN, DOWN: UP}


@dataclass
class Quote:
    price: float
    shares: float


def floor_tick(price: float, tick: float) -> float:
    return math.floor(price / tick + 1e-9) * tick


class MakerQuoter:
    def __init__(self, cfg: MakerConfig):
        self.cfg = cfg

    @staticmethod
    def market_fair_up(books: dict[str, Book]) -> float | None:
        """The market's own view of P(Up): both books, averaged."""
        mu, md = books[UP].mid, books[DOWN].mid
        if mu is not None and md is not None:
            return (mu + 1.0 - md) / 2
        if mu is not None:
            return mu
        if md is not None:
            return 1.0 - md
        return None

    def half_spread(self, prob_vol_1s: float | None) -> float:
        """Distance below fair: at least `half_spread`, and wider when the
        probability itself is moving fast. Near the strike a 5-minute binary's
        price can swing ~3c in one second; a bid closer than a couple of
        seconds' worth of that gets picked off before we can move it."""
        c = self.cfg
        if prob_vol_1s is None:
            return c.half_spread
        return max(c.half_spread, c.vol_spread_mult * prob_vol_1s * math.sqrt(c.reaction_s))

    def targets(
        self, books: dict[str, Book], pos: WindowPosition, model_p_up: float | None,
        prob_vol_1s: float | None = None,
    ) -> tuple[dict[str, Quote | None], str]:
        """Where we want a bid on each side right now (None = no bid), and why."""
        c = self.cfg
        hs = self.half_spread(prob_vol_1s)
        fair_up = self.market_fair_up(books)
        if fair_up is None:
            return {UP: None, DOWN: None}, "no market prices"
        if model_p_up is not None and abs(model_p_up - fair_up) > c.max_model_gap:
            return {UP: None, DOWN: None}, f"model {model_p_up:.2f} vs market {fair_up:.2f}: disagree, staying out"

        fair = {UP: fair_up, DOWN: 1.0 - fair_up}
        imbalance = pos.shares[UP] - pos.shares[DOWN]  # >0: long Up, need Down to pair
        out: dict[str, Quote | None] = {}
        notes = []
        for side in (UP, DOWN):
            held, other_held = pos.shares[side], pos.shares[OTHER[side]]
            lean = imbalance if side == UP else -imbalance  # >0: this side is the heavy one
            price = fair[side] - hs - c.skew_per_share * lean

            if other_held > held:
                # This bid completes pairs: never pay more than 1 - their cost - margin.
                avg_other = pos.cost_usd[OTHER[side]] / other_held
                price = min(price, 1.0 - avg_other - c.pair_margin)
            room = c.max_side_shares - held
            if lean >= 0:
                room = min(room, c.max_imbalance_shares - lean)
            if room < 1:
                out[side] = None
                notes.append(f"{side}: inventory limit")
                continue

            book = books[side]
            if book.best_bid is not None:
                price = min(price, book.best_bid + c.tick)  # top of book, but no higher than needed
            if book.best_ask is not None:
                price = min(price, book.best_ask - c.tick)  # post-only: never cross
            price = floor_tick(price, c.tick)
            if not (c.min_price <= price <= c.max_price):
                out[side] = None
                notes.append(f"{side}: price {price:.2f} out of range")
                continue
            out[side] = Quote(round(price, 4), min(c.quote_shares, room))
        q_up, q_dn = out[UP], out[DOWN]
        pair = f" (pair {q_up.price + q_dn.price:.2f})" if q_up and q_dn else ""
        why = (
            f"quoting Up {q_up.price:.2f} / Down {q_dn.price:.2f}{pair}" if q_up and q_dn
            else f"quoting Up {q_up.price:.2f}" if q_up
            else f"quoting Down {q_dn.price:.2f}" if q_dn
            else "no quotes"
        )
        if notes:
            why += " | " + "; ".join(notes)
        return out, why


@dataclass
class RestingOrder:
    token: str
    outcome: str
    price: float
    shares_left: float
    placed_ts: float
    order_id: str | None = None
    matched: float = 0.0  # live: size matched so far, to spot new fills
    seen: set = None  # paper: trade prints already counted against this order

    def __post_init__(self):
        if self.seen is None:
            self.seen = set()


@dataclass
class MakerFill:
    token: str
    outcome: str
    price: float
    shares: float


class PaperMakerBroker:
    """Simulated resting orders. A paper bid fills when either:

    1. a public trade shows a taker SELLING at or below our price after we
       placed the bid: that seller would have hit us (this is the ordinary,
       profitable flow a market maker lives on), or
    2. the market's best ask comes down to our price: a seller reached our
       level, typically because the price moved against that side (the
       risky, adverse kind of fill).

    Queue position is ignored: if others sat at our exact price first, some
    of those fills would have gone to them. Treat paper as optimistic on
    fill count and judge it by the P&L of what did fill.
    """

    live = False

    def __init__(self, cfg: MakerConfig):
        self.cfg = cfg
        self.orders: dict[str, RestingOrder] = {}

    def sync(self, token: str, outcome: str, quote: Quote | None, now: float) -> None:
        cur = self.orders.get(token)
        if quote is None:
            if cur:
                self._cancel(cur)
            return
        if cur:
            moved = abs(cur.price - quote.price) >= self.cfg.requote_ticks * self.cfg.tick - 1e-9
            if not moved:
                return
            # Backing off (lower bid) is urgent: a stale high bid is exactly
            # what gets picked off. Only moving UP is throttled.
            if quote.price > cur.price and now - cur.placed_ts < self.cfg.requote_s:
                return
            self._cancel(cur)
        self._place(RestingOrder(token, outcome, quote.price, quote.shares, now))

    def _place(self, order: RestingOrder) -> None:
        self.orders[order.token] = order

    def _cancel(self, order: RestingOrder) -> None:
        self.orders.pop(order.token, None)

    def cancel_all(self, tokens: list[str]) -> None:
        for t in tokens:
            if t in self.orders:
                self._cancel(self.orders[t])

    def fills(self, token: str, book: Book | None, now: float, trades=None) -> list[MakerFill]:
        o = self.orders.get(token)
        if o is None or now <= o.placed_ts:
            return []
        volume = 0.0
        for t in trades or []:
            if t.token == token and t.side == "SELL" and t.ts > o.placed_ts and t.price <= o.price + 1e-9 and t.key not in o.seen:
                o.seen.add(t.key)
                volume += t.size
        if book is not None:
            volume += sum(l.size for l in book.asks if l.price <= o.price + 1e-9)
        if volume <= 0:
            return []
        shares = min(o.shares_left, volume)
        o.shares_left -= shares
        if o.shares_left < 0.01:
            self.orders.pop(token, None)
        return [MakerFill(token, o.outcome, o.price, shares)]


class LiveMakerBroker(PaperMakerBroker):
    """Real post-only GTC orders. Fills are read back from the order's
    size_matched. UNTESTED against the live API: watch it closely with small
    sizes before trusting it."""

    live = True

    def __init__(self, cfg: MakerConfig, client):
        super().__init__(cfg)
        self.client = client
        self._last_poll: dict[str, float] = {}

    def _place(self, order: RestingOrder) -> None:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        try:
            signed = self.client.create_order(OrderArgs(token_id=order.token, price=order.price, size=round(order.shares_left, 2), side=BUY))
            resp = self.client.post_order(signed, OrderType.GTC, post_only=True)
        except Exception:
            logger.exception("post-only order failed for %s @ %.2f", order.outcome, order.price)
            return
        order.order_id = (resp or {}).get("orderID") or (resp or {}).get("orderId")
        if not order.order_id:
            logger.warning("order not accepted: %s", resp)
            return
        self.orders[order.token] = order

    def _cancel(self, order: RestingOrder) -> None:
        self.orders.pop(order.token, None)
        if order.order_id:
            try:
                self.client.cancel(order.order_id)
            except Exception:
                logger.exception("cancel failed for %s (%s); cancel it in the Polymarket UI if it stays open", order.outcome, order.order_id)

    def fills(self, token: str, book: Book | None, now: float, trades=None) -> list[MakerFill]:
        o = self.orders.get(token)
        if o is None or not o.order_id or now - self._last_poll.get(token, 0) < 2.0:
            return []
        self._last_poll[token] = now
        try:
            info = self.client.get_order(o.order_id) or {}
            matched = float(info.get("size_matched") or 0)
        except Exception:
            return []
        new = matched - o.matched
        if new <= 1e-9:
            return []
        o.matched = matched
        o.shares_left = max(0.0, o.shares_left - new)
        if o.shares_left < 0.01 or str(info.get("status", "")).upper() in ("MATCHED", "CANCELED"):
            self.orders.pop(token, None)
        return [MakerFill(token, o.outcome, o.price, new)]


class FastMoveGuard:
    """Pull quotes for a while after a sharp move in the coin's price."""

    def __init__(self, cfg: MakerConfig):
        self.cfg = cfg
        self.until: dict[str, float] = {}

    def check(self, asset: str, now: float, price_now: float, price_then: float | None, sigma: float) -> bool:
        """True while quotes should stay pulled."""
        c = self.cfg
        if price_then and price_then > 0 and sigma > 0:
            z = abs(math.log(price_now / price_then)) / (sigma * math.sqrt(c.fast_move_window_s))
            if z > c.fast_move_z:
                self.until[asset] = now + c.fast_move_cooldown_s
        return now < self.until.get(asset, 0.0)
