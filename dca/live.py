"""Live broker: places real orders on Binance futures.

Safety orders and the take profit rest on the exchange as limit orders, so
fills happen even if the bot is briefly offline. Each sync reconciles the
deal against Binance's order and position state.
"""
from __future__ import annotations

import logging

from dca.config import DcaSettings
from dca.deal import Deal
from dca.events import EXTERNAL_CLOSE, OPEN, SAFETY, STOP_LOSS, TAKE_PROFIT, Event
from dca.exchange import BinanceFutures
from dca.ladder import LONG, build_plan
from dca.paper import new_deal

log = logging.getLogger("dcabot")

_EPS = 1e-9


class LiveBroker:
    def __init__(self, settings: DcaSettings, ex: BinanceFutures):
        self.settings = settings
        self.ex = ex
        self.mode = "demo" if settings.demo_trading else "live"

    # --- open -------------------------------------------------------------

    def open_deal(self, side: str, price: float, now: str) -> tuple[Deal | None, list[Event]]:
        existing = self.ex.position_qty(side)
        if existing > 0:
            log.error(
                "%s: Binance already has a %s position of %.4f that this bot is not tracking — "
                "close it manually; not opening a new deal on this side.", side, side, existing,
            )
            return None, []

        ladder = self.settings.ladder
        plan = build_plan(price, side, ladder.levels(), ladder.take_profit_pct, ladder.stop_loss_pct,
                          self.settings.leverage, self.settings.maintenance_margin_rate)
        free = self.ex.free_usdt()
        if free < plan.margin_usdt * 1.05:
            log.warning("%s: free balance $%.2f < worst-case margin $%.2f — skipping entry",
                        side, free, plan.margin_usdt)
            return None, []

        base_qty = self.ex.amount(ladder.base_order_usdt / price)
        order = self.ex.market_open(side, base_qty)
        filled = float(order.get("filled") or 0.0)
        if filled <= 0:
            order = self.ex.fetch_order(order["id"])
            filled = float(order.get("filled") or 0.0)
        if filled <= 0:
            log.error("%s: base market order %s reported no fill", side, order.get("id"))
            return None, []
        fill_price = float(order.get("average") or price)

        # Rebuild the ladder from the real fill price.
        deal = new_deal(self.settings, side, fill_price, now)
        base = deal.orders[0]
        base.qty = base.filled_qty = filled
        base.fill_price = fill_price
        base.order_id = str(order["id"])
        deal.fees_usdt += fill_price * filled * self.settings.taker_fee
        deal.sl_price = self.ex.price(deal.sl_price)

        min_notional = self.ex.min_notional()
        for o in deal.orders[1:]:
            o.price = self.ex.price(o.price)
            o.qty = self.ex.amount(o.qty)
            if o.qty * o.price < min_notional:
                log.warning("%s: safety order #%d below min notional — skipped", side, o.index)
                o.qty = 0.0
                continue
            o.order_id = str(self.ex.limit_open(side, o.qty, o.price)["id"])
        deal.orders = [deal.orders[0]] + [o for o in deal.orders[1:] if o.qty > 0]

        deal.recompute_tp()
        self._refresh_exits(deal)
        return deal, [Event(OPEN, fill_price, filled, "base order (market)")]

    # --- sync -------------------------------------------------------------

    def sync(self, deal: Deal, low: float, high: float, last: float, now: str) -> list[Event]:
        events: list[Event] = []

        if deal.tp_order_id:
            tp = self.ex.fetch_order(deal.tp_order_id)
            if tp.get("status") == "closed":
                exit_price = float(tp.get("average") or deal.tp_price)
                qty = deal.filled_qty
                self._flatten(deal)
                deal.close(exit_price, TAKE_PROFIT, now, exit_price * qty * self.settings.maker_fee)
                return [Event(TAKE_PROFIT, exit_price, qty, "take profit")]

        for o in deal.pending_safety_orders():
            if not o.order_id:
                continue
            info = self.ex.fetch_order(o.order_id)
            filled = float(info.get("filled") or 0.0)
            if filled > o.filled_qty + _EPS:
                new_qty = filled - o.filled_qty
                o.fill_price = float(info.get("average") or o.price)
                o.filled_qty = filled
                deal.fees_usdt += o.fill_price * new_qty * self.settings.maker_fee
                events.append(Event(SAFETY, o.fill_price, new_qty, f"safety order #{o.index}"))
        if events:
            deal.recompute_tp()
            self._refresh_exits(deal)

        sl_hit = last <= deal.sl_price if deal.side == LONG else last >= deal.sl_price
        if sl_hit:
            qty = self.ex.position_qty(deal.side)
            exit_price = last
            if qty > 0:
                order = self.ex.market_close(deal.side, self.ex.amount(qty))
                exit_price = float(order.get("average") or last)
            self._flatten(deal)
            deal.close(exit_price, STOP_LOSS, now, exit_price * deal.filled_qty * self.settings.taker_fee)
            events.append(Event(STOP_LOSS, exit_price, deal.filled_qty, "stop loss (bot)"))
            return events

        if self.ex.position_qty(deal.side) <= deal.filled_qty * 0.01:
            # Exchange-side SL fired, liquidation, or a manual close in the Binance app.
            self._flatten(deal)
            deal.close(last, EXTERNAL_CLOSE, now, last * deal.filled_qty * self.settings.taker_fee)
            events.append(Event(EXTERNAL_CLOSE, last, deal.filled_qty,
                                "position gone on Binance (exchange SL / manual / liquidation); PnL estimated"))
        return events

    # --- helpers ----------------------------------------------------------

    def _refresh_exits(self, deal: Deal) -> None:
        """(Re)place TP and exchange-side SL for the current position size."""
        qty = self.ex.amount(self.ex.position_qty(deal.side) or deal.filled_qty)
        self.ex.cancel(deal.tp_order_id)
        deal.tp_order_id = str(self.ex.limit_close(deal.side, qty, self.ex.price(deal.tp_price))["id"])
        deal.tp_order_qty = qty
        if self.settings.risk.exchange_stop_loss:
            self.ex.cancel(deal.sl_order_id, trigger=True)
            try:
                deal.sl_order_id = str(self.ex.stop_close(deal.side, qty, deal.sl_price)["id"])
            except Exception:
                deal.sl_order_id = None
                log.exception("%s: could not place exchange-side SL; bot-side SL still active", deal.side)

    def _flatten(self, deal: Deal) -> None:
        """Cancel every order of the deal and market-close any leftover position."""
        for o in deal.orders[1:]:
            self.ex.cancel(o.order_id)
        self.ex.cancel(deal.tp_order_id)
        self.ex.cancel(deal.sl_order_id, trigger=True)
        leftover = self.ex.position_qty(deal.side)
        if leftover > 0:
            log.warning("%s: closing leftover position %.4f at market", deal.side, leftover)
            self.ex.market_close(deal.side, self.ex.amount(leftover))
