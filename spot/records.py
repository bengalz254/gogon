"""What the grid bot keeps on disk: its state (JSON) and a trade log (CSV)."""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone

from spot.grid import Fill

TRADE_FIELDS = ["timestamp", "mode", "symbol", "side", "price", "amount", "value", "fee", "tax", "pnl", "slot", "note"]


class JsonStore:
    """A JSON document written atomically (a crash never leaves half a file)."""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict | None:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def archive(self) -> str | None:
        """Move the file aside (timestamped); returns the new path, or None if there was none."""
        if not os.path.exists(self.path):
            return None
        backup = f"{self.path}.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
        os.replace(self.path, backup)
        return backup


class TradeLog:
    """Append-only CSV of every simulated fill."""

    def __init__(self, path: str):
        self.path = path

    def record(self, fill: Fill, symbol: str, mode: str, note: str = "") -> None:
        new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            if new:
                writer.writeheader()
            writer.writerow(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "mode": mode,
                    "symbol": symbol,
                    "side": fill.side,
                    "price": f"{fill.price:.8f}".rstrip("0").rstrip("."),
                    "amount": f"{fill.amount:.8f}",
                    "value": f"{fill.value:.4f}",
                    "fee": f"{fill.fee:.4f}",
                    "tax": f"{fill.tax:.4f}",
                    "pnl": f"{fill.pnl:.4f}" if fill.side == "SELL" else "",
                    "slot": fill.slot,
                    "note": note,
                }
            )

    def tail(self, count: int) -> list[dict]:
        try:
            with open(self.path, newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))[-count:]
        except FileNotFoundError:
            return []
