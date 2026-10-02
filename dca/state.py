"""Persists open deals and daily P&L so a restart picks up where it left off."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from dca.deal import Deal


@dataclass
class EngineState:
    deals: dict[str, Deal] = field(default_factory=dict)  # side -> open deal
    daily_pnl: dict[str, float] = field(default_factory=dict)  # "YYYY-MM-DD" -> USDT
    cooldown_until: dict[str, str] = field(default_factory=dict)  # side -> ISO time
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0

    def to_dict(self) -> dict:
        return {
            "deals": {s: d.to_dict() for s, d in self.deals.items()},
            "daily_pnl": self.daily_pnl,
            "cooldown_until": self.cooldown_until,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "EngineState":
        return cls(
            deals={s: Deal.from_dict(d) for s, d in (raw.get("deals") or {}).items()},
            daily_pnl=dict(raw.get("daily_pnl") or {}),
            cooldown_until=dict(raw.get("cooldown_until") or {}),
            wins=int(raw.get("wins", 0)),
            losses=int(raw.get("losses", 0)),
            total_pnl=float(raw.get("total_pnl", 0.0)),
        )


class StateStore:
    def __init__(self, path: str | None):
        self.path = path

    def load(self) -> EngineState:
        if not self.path or not os.path.exists(self.path):
            return EngineState()
        with open(self.path, "r", encoding="utf-8") as f:
            return EngineState.from_dict(json.load(f))

    def save(self, state: EngineState) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, indent=2)
        os.replace(tmp, self.path)  # atomic: never leaves a half-written state file
