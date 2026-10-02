"""CSV journal of every DCA event (open, safety fills, closes)."""
from __future__ import annotations

import csv
import os

from dca.deal import Deal
from dca.events import Event

FIELDS = [
    "timestamp", "mode", "deal_id", "side", "event", "price", "qty", "notional_usdt",
    "avg_price", "position_qty", "safety_orders_filled", "pnl_usdt", "reason",
]


class DcaJournal:
    def __init__(self, path: str | None = "data/dca_trades.csv"):
        self.path = path
        if path and not os.path.exists(path):
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(FIELDS)

    def record(self, now: str, mode: str, deal: Deal, event: Event) -> None:
        if not self.path:
            return
        closed = deal.status == "closed"
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                now, mode, deal.deal_id, deal.side, event.kind,
                f"{event.price:.4f}", f"{event.qty:.4f}", f"{event.price * event.qty:.2f}",
                f"{deal.avg_price:.4f}", f"{0.0 if closed else deal.filled_qty:.4f}",
                deal.safety_orders_filled, f"{deal.realized_pnl:.4f}" if closed else "",
                event.reason,
            ])
