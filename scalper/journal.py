"""Closed-trade journal (CSV) and crash-safe JSON state file."""
from __future__ import annotations

import csv
import json
import os
import time

FIELDS = [
    "closed_at_utc",
    "mode",
    "trade_id",
    "symbol",
    "side",
    "strategy",
    "entry_time_utc",
    "exit_time_utc",
    "entry_price",
    "exit_price",
    "qty",
    "initial_stop",
    "take_profit",
    "exit_reason",
    "gross_pnl",
    "fees",
    "funding",
    "net_pnl",
    "r_multiple",
    "bars_held",
    "equity_after",
    "approximate",
    "note",
]


def utc(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000)) if ms else ""


class TradeJournal:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(FIELDS)

    def record(self, row: dict) -> None:
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore").writerow(row)

    def read(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))


class StateStore:
    """JSON state written atomically (temp file + rename), so a crash or power
    loss mid-write can never leave a half-written, unreadable state file."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            # Unreadable JSON: keep a copy for inspection and start clean. Open
            # positions are then re-discovered from the exchange (reconcile).
            backup = f"{self.path}.corrupt.{int(time.time())}"
            os.replace(self.path, backup)
            return {}

    def peek(self) -> dict:
        """Read without side effects (for the dashboard / status command)."""
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
