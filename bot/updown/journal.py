"""Up/Down journals.

- data/trades.csv            fills (BUY) and settlements (SETTLE/ADJUST), in the
                             same format as the classic bot, so the existing
                             dashboard keeps working.
- data/updown_decisions.jsonl every intent a strategy produced, accepted or
                             rejected, with the full model inputs and the reason.
- data/updown_snapshots.jsonl periodic model-vs-market snapshots per window,
                             the raw material for calibration (updown_report.py).
- data/updown_settlements.csv one row per window outcome: computed TWAP and
                             last-price settlement vs the official result, and
                             P&L per strategy.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone

from bot.journal import TradeJournal

SETTLEMENT_FIELDS = [
    "timestamp", "mode", "window_id", "asset", "start", "end", "ptb", "ptb_source",
    "settle_twap", "settle_last", "winner_twap_rule", "winner_last_rule",
    "provisional_winner", "official_winner", "booked_winner", "event", "pnl_total", "pnl_by_strategy",
]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class UpDownJournal:
    def __init__(self, data_dir: str = "data", trades_csv: str | None = None, mode: str = "paper"):
        self.mode = mode
        os.makedirs(data_dir, exist_ok=True)
        self.trades = TradeJournal(trades_csv or os.path.join(data_dir, "trades.csv"))
        self._decisions = open(os.path.join(data_dir, "updown_decisions.jsonl"), "a", buffering=1, encoding="utf-8")
        self._snapshots = open(os.path.join(data_dir, "updown_snapshots.jsonl"), "a", buffering=1, encoding="utf-8")
        self._settle_path = os.path.join(data_dir, "updown_settlements.csv")
        if not os.path.exists(self._settle_path):
            with open(self._settle_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(SETTLEMENT_FIELDS)

    # -- trade log (dashboard compatible) --------------------------------------
    def fill(self, *, ts: float, strategy: str, window_id: str, token_id: str, outcome: str,
             price: float, shares: float, fee: float, reason: str) -> None:
        self.trades.record_row(
            mode=self.mode, strategy=strategy, market_id=window_id, token_id=token_id,
            outcome=outcome, side="BUY", price=price, size_shares=shares,
            size_usd=shares * price + fee, filled=True, group_id=window_id,
            reason=reason, timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
        )

    def settle(self, *, ts: float, strategy: str, window_id: str, token_id: str, outcome: str,
               shares: float, won: bool, winner: str) -> None:
        self.trades.record_row(
            mode=self.mode, strategy=strategy, market_id=window_id, token_id=token_id,
            outcome=outcome, side="SETTLE", price=1.0 if won else 0.0, size_shares=shares,
            size_usd=shares if won else 0.0, filled=True, group_id=window_id,
            reason=f"settlement: {winner} won", timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
        )

    def adjust(self, *, ts: float, strategy: str, window_id: str, token_id: str, outcome: str,
               pnl_delta: float, reason: str) -> None:
        self.trades.record_row(
            mode=self.mode, strategy=strategy, market_id=window_id, token_id=token_id,
            outcome=outcome, side="ADJUST", price=0.0, size_shares=0.0, size_usd=pnl_delta,
            filled=True, group_id=window_id, reason=reason,
            timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
        )

    # -- research logs -------------------------------------------------------------------
    def decision(self, row: dict) -> None:
        self._decisions.write(json.dumps(row, default=float) + "\n")

    def snapshot(self, row: dict) -> None:
        self._snapshots.write(json.dumps(row, default=float) + "\n")

    def settlement(self, row: dict) -> None:
        row = dict(row)
        row.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        row.setdefault("mode", self.mode)
        for k in ("start", "end"):
            if isinstance(row.get(k), (int, float)):
                row[k] = _iso(row[k])
        if isinstance(row.get("pnl_by_strategy"), dict):
            row["pnl_by_strategy"] = json.dumps(row["pnl_by_strategy"])
        with open(self._settle_path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=SETTLEMENT_FIELDS, extrasaction="ignore").writerow(row)

    def close(self) -> None:
        for fh in (self._decisions, self._snapshots):
            try:
                fh.close()
            except Exception:
                pass


class NullJournal:
    """Drop-in journal that records nothing (unit tests)."""

    mode = "test"

    def __getattr__(self, name):
        return lambda *a, **kw: None
