"""Broker-agnostic DCA engine: manages one deal per side, entries, and risk."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from dca.config import DcaSettings
from dca.events import EXTERNAL_CLOSE, STOP_LOSS, Event
from dca.journal import DcaJournal
from dca.signals import entry_signal
from dca.state import EngineState, StateStore

log = logging.getLogger("dcabot")


class DcaEngine:
    def __init__(self, settings: DcaSettings, broker, store: StateStore, journal: DcaJournal):
        self.settings = settings
        self.broker = broker
        self.store = store
        self.journal = journal
        self.state: EngineState = store.load()
        self.closed_deals: list = []  # deals closed during this run (used by the backtester)

    # --- risk gates -------------------------------------------------------

    def pnl_today(self, now: datetime) -> float:
        return self.state.daily_pnl.get(now.date().isoformat(), 0.0)

    def can_open(self, side: str, now: datetime) -> tuple[bool, str]:
        if self.pnl_today(now) <= -self.settings.risk.max_daily_loss_usdt:
            return False, f"daily loss limit hit (${self.pnl_today(now):.2f})"
        until = self.state.cooldown_until.get(side)
        if until and now < datetime.fromisoformat(until):
            return False, f"cooldown until {until}"
        return True, ""

    # --- main step --------------------------------------------------------

    def tick(self, now: datetime, closes: list[float], low: float, high: float, last: float) -> None:
        ts = now.isoformat()
        for side in self.settings.sides:
            deal = self.state.deals.get(side)
            if deal is None:
                continue
            for ev in self.broker.sync(deal, low, high, last, ts):
                self._on_event(ts, deal, ev)
            if deal.status == "closed":
                self._on_close(now, deal)
                del self.state.deals[side]

        for side in self.settings.sides:
            if side in self.state.deals:
                continue
            ok, why = self.can_open(side, now)
            if not ok:
                log.debug("%s: not opening — %s", side, why)
                continue
            ok, why = entry_signal(side, closes, self.settings.entry)
            if not ok:
                log.debug("%s: no entry — %s", side, why)
                continue
            deal, events = self.broker.open_deal(side, last, ts)
            if deal is None:
                continue
            self.state.deals[side] = deal
            for ev in events:
                ev.reason = f"{ev.reason}; {why}"
                self._on_event(ts, deal, ev)

        self.store.save(self.state)

    def _on_event(self, ts: str, deal, ev: Event) -> None:
        self.journal.record(ts, self.broker.mode, deal, ev)
        log.info(
            "[%s] %s %s @ %.4f qty=%.4f | avg=%.4f pos=%.4f SO=%d/%d TP=%.4f SL=%.4f%s",
            deal.side.upper(), deal.deal_id, ev.kind, ev.price, ev.qty, deal.avg_price,
            deal.filled_qty, deal.safety_orders_filled, len(deal.orders) - 1, deal.tp_price,
            deal.sl_price,
            f" | PnL ${deal.realized_pnl:.2f}" if deal.status == "closed" else "",
        )

    def _on_close(self, now: datetime, deal) -> None:
        self.closed_deals.append(deal)
        day = now.date().isoformat()
        self.state.daily_pnl[day] = self.state.daily_pnl.get(day, 0.0) + deal.realized_pnl
        self.state.total_pnl += deal.realized_pnl
        if deal.realized_pnl >= 0:
            self.state.wins += 1
        else:
            self.state.losses += 1
        if deal.close_reason in (STOP_LOSS, EXTERNAL_CLOSE):
            until = now + timedelta(minutes=self.settings.risk.cooldown_minutes_after_stop)
            self.state.cooldown_until[deal.side] = until.isoformat()
