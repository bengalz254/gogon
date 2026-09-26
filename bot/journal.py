"""CSV trade journal — an audit trail of every signal the bot acted on."""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone

from bot.strategies.base import Signal

logger = logging.getLogger("polybot.journal")

FIELDS = [
    "timestamp",
    "mode",
    "strategy",
    "market_id",
    "token_id",
    "outcome",
    "side",
    "price",
    "size_shares",
    "size_usd",
    "fee_usd",
    "filled",
    "group_id",
    "reason",
]


class TradeJournal:
    def __init__(self, path: str = "data/trades.csv"):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not os.path.exists(path):
            self._write_header()
        else:
            self._upgrade_header_if_needed()

    def _write_header(self) -> None:
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(FIELDS)

    def _upgrade_header_if_needed(self) -> None:
        """Bring a log written by an older version up to the current columns.

        Columns added since (like fee_usd) are left blank on old rows. A file
        with columns this version doesn't know is set aside, never overwritten.
        """
        with open(self.path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), None)
        if header == FIELDS:
            return
        if not header:
            self._write_header()
            return
        if not set(header) <= set(FIELDS):
            backup = f"{self.path}.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
            os.replace(self.path, backup)
            logger.warning("Trade log had unknown columns; moved it to %s and started a new one", backup)
            self._write_header()
            return

        with open(self.path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, restval="")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, self.path)
        logger.info("Upgraded trade log %s to the current column layout", self.path)

    def record(self, signal: Signal, mode: str, filled: bool) -> None:
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                [
                    datetime.now(timezone.utc).isoformat(),
                    mode,
                    signal.strategy,
                    signal.market_id,
                    signal.token_id,
                    signal.outcome,
                    signal.side,
                    f"{signal.limit_price:.4f}",
                    f"{signal.size_shares:.4f}",
                    f"{signal.size_usd:.4f}",
                    f"{signal.fee_usd:.4f}",
                    filled,
                    signal.group_id or "",
                    signal.reason,
                ]
            )
