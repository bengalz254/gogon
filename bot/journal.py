"""CSV trade journal — an audit trail of every signal the bot acted on."""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone

from bot.strategies.base import Signal

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
    "filled",
    "group_id",
    "reason",
]


class TradeJournal:
    def __init__(self, path: str = "data/trades.csv"):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(FIELDS)

    def record(self, signal: Signal, mode: str, filled: bool) -> None:
        self.record_row(
            mode=mode,
            strategy=signal.strategy,
            market_id=signal.market_id,
            token_id=signal.token_id,
            outcome=signal.outcome,
            side=signal.side,
            price=signal.limit_price,
            size_shares=signal.size_shares,
            size_usd=signal.size_usd,
            filled=filled,
            group_id=signal.group_id,
            reason=signal.reason,
        )

    def record_row(
        self,
        *,
        mode: str,
        strategy: str,
        market_id: str,
        token_id: str,
        outcome: str,
        side: str,
        price: float,
        size_shares: float,
        size_usd: float,
        filled: bool,
        group_id: str | None,
        reason: str,
        timestamp: datetime | None = None,
    ) -> None:
        """Append one row. `side` is BUY, SELL, or SETTLE (a position resolved
        at $1/$0 per share when its market settles)."""
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                [
                    (timestamp or datetime.now(timezone.utc)).isoformat(),
                    mode,
                    strategy,
                    market_id,
                    token_id,
                    outcome,
                    side,
                    f"{price:.4f}",
                    f"{size_shares:.4f}",
                    f"{size_usd:.4f}",
                    filled,
                    group_id or "",
                    reason,
                ]
            )
