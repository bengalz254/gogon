"""Crash-safe persistence of the bot's risk state (open positions and today's
realized P&L) in SQLite.

A bot that runs 24/7 will restart — deploys, crashes, VPS reboots. Without
this, every restart forgot the open positions, so exposure caps silently
reset to zero while the real positions were still on the exchange.

Paper and live trading use separate database files so simulated positions
never count against real limits.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime

from bot.risk import Position, RiskState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    token_id      TEXT PRIMARY KEY,
    market_id     TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    size          REAL NOT NULL,
    cost_usd      REAL NOT NULL,
    opened_at     TEXT NOT NULL,
    outcome_count INTEGER
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def state_path(live: bool, data_dir: str = "data") -> str:
    return os.path.join(data_dir, "state_live.sqlite3" if live else "state_paper.sqlite3")


class StateStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path)
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def save(self, state: RiskState) -> None:
        """Replace the stored state with `state` in one transaction, so a crash
        mid-save leaves the previous state intact rather than a half-written one."""
        with self._conn:
            self._conn.execute("DELETE FROM positions")
            self._conn.executemany(
                "INSERT INTO positions (token_id, market_id, outcome, size, cost_usd, opened_at, outcome_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (p.token_id, p.market_id, p.outcome, p.size, p.cost_usd, p.opened_at.isoformat(), p.outcome_count)
                    for p in state.positions
                ],
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                [("day", state.day.isoformat()), ("realized_pnl_today", repr(state.realized_pnl_today))],
            )

    def load(self) -> RiskState | None:
        """The last saved state, or None if nothing was ever saved."""
        meta = dict(self._conn.execute("SELECT key, value FROM meta"))
        if "day" not in meta:
            return None
        rows = self._conn.execute(
            "SELECT token_id, market_id, outcome, size, cost_usd, opened_at, outcome_count "
            "FROM positions ORDER BY token_id"
        )
        positions = [
            Position(
                market_id=market_id,
                token_id=token_id,
                outcome=outcome,
                size=size,
                cost_usd=cost_usd,
                opened_at=datetime.fromisoformat(opened_at),
                outcome_count=outcome_count,
            )
            for token_id, market_id, outcome, size, cost_usd, opened_at, outcome_count in rows
        ]
        return RiskState(
            day=date.fromisoformat(meta["day"]),
            realized_pnl_today=float(meta.get("realized_pnl_today", 0.0)),
            positions=positions,
        )

    def close(self) -> None:
        self._conn.close()
