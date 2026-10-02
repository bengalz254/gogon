"""Simulated broker for paper trading and backtests (no orders sent)."""
from __future__ import annotations

import uuid

from dca.config import DcaSettings
from dca.deal import Deal, Order
from dca.events import OPEN, SAFETY, STOP_LOSS, TAKE_PROFIT, Event
from dca.ladder import LONG, build_plan


def new_deal(settings: DcaSettings, side: str, entry: float, now: str) -> Deal:
    ladder = settings.ladder
    plan = build_plan(
        entry, side, ladder.levels(), ladder.take_profit_pct, ladder.stop_loss_pct,
        settings.leverage, settings.maintenance_margin_rate,
    )
    return Deal(
        deal_id=f"{side}-{uuid.uuid4().hex[:8]}",
        side=side,
        entry_price=entry,
        take_profit_pct=ladder.take_profit_pct,
        sl_price=plan.sl_price,
        opened_at=now,
        orders=[Order(index=r.index, price=r.price, qty=r.qty) for r in plan.rows],
    )


class PaperBroker:
    mode = "paper"

    def __init__(self, settings: DcaSettings):
        self.settings = settings

    def open_deal(self, side: str, price: float, now: str) -> tuple[Deal, list[Event]]:
        deal = new_deal(self.settings, side, price, now)
        base = deal.orders[0]
        base.filled_qty = base.qty
        base.fill_price = price
        deal.fees_usdt += price * base.qty * self.settings.taker_fee
        deal.recompute_tp()
        return deal, [Event(OPEN, price, base.qty, "base order (market)")]

    def sync(self, deal: Deal, low: float, high: float, last: float, now: str) -> list[Event]:
        """Advance the deal through a price range [low, high].

        Conservative ordering: safety orders fill first, then SL is checked;
        TP only fills on a bar where no safety order filled.
        """
        events: list[Event] = []
        is_long = deal.side == LONG
        for o in deal.pending_safety_orders():
            reached = low <= o.price if is_long else high >= o.price
            if not reached:
                break  # orders are ordered by distance; later ones are further away
            o.filled_qty = o.qty
            o.fill_price = o.price
            deal.fees_usdt += o.price * o.qty * self.settings.maker_fee
            events.append(Event(SAFETY, o.price, o.qty, f"safety order #{o.index}"))
        if events:
            deal.recompute_tp()

        sl_hit = low <= deal.sl_price if is_long else high >= deal.sl_price
        if sl_hit:
            # If the whole bar gapped past SL, fill at the bar's best price instead.
            exit_price = min(deal.sl_price, high) if is_long else max(deal.sl_price, low)
            qty = deal.filled_qty
            deal.close(exit_price, STOP_LOSS, now, exit_price * qty * self.settings.taker_fee)
            events.append(Event(STOP_LOSS, exit_price, qty, "stop loss"))
            return events

        if not events:
            tp_hit = high >= deal.tp_price if is_long else low <= deal.tp_price
            if tp_hit:
                qty = deal.filled_qty
                deal.close(deal.tp_price, TAKE_PROFIT, now, deal.tp_price * qty * self.settings.maker_fee)
                events.append(Event(TAKE_PROFIT, deal.tp_price, qty, "take profit"))
        return events
