"""Simulated wallet: cash, open positions, and a CSV of every buy and sell."""
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

JOURNAL_FIELDS = [
    "time", "action", "mint", "symbol", "mood", "price", "usd",
    "pnl_usd", "pnl_pct", "reason",
]


@dataclass
class Position:
    mint: str
    symbol: str
    mood: str
    entry_price: float
    qty: float
    cost_usd: float
    opened_at: float
    last_price: float = 0.0
    last_price_at: float = 0.0


class Paper:
    def __init__(self, data_dir: Path, starting_cash: float, fee_pct: float):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.journal_path = self.dir / "trades.csv"
        self.fee = fee_pct / 100
        self.cash = starting_cash
        self.starting_cash = starting_cash
        self.positions: dict[str, Position] = {}
        self.cooldowns: dict[str, float] = {}
        self.buy_times: list[float] = []
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.cash = float(raw.get("cash", self.cash))
        self.positions = {m: Position(**p) for m, p in (raw.get("positions") or {}).items()}
        self.cooldowns = {m: float(t) for m, t in (raw.get("cooldowns") or {}).items()}
        self.buy_times = [float(t) for t in raw.get("buy_times") or []]
        # Raising starting_cash_usd in the config tops the simulated wallet up by the difference.
        recorded = float(raw.get("starting_cash", 100.0))
        if self.starting_cash > recorded:
            self.cash += self.starting_cash - recorded
        else:
            self.starting_cash = recorded

    def save(self, now: float | None = None) -> None:
        now = now or time.time()
        self.cooldowns = {m: t for m, t in self.cooldowns.items() if t > now}
        self.buy_times = [t for t in self.buy_times if t > now - 3600]
        data = {
            "cash": self.cash,
            "starting_cash": self.starting_cash,
            "positions": {m: asdict(p) for m, p in self.positions.items()},
            "cooldowns": self.cooldowns,
            "buy_times": self.buy_times,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _journal(self, row: dict) -> None:
        new = not self.journal_path.exists()
        with self.journal_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)

    def buy(self, mint: str, symbol: str, mood: str, price: float, usd: float, now: float | None = None) -> Position:
        now = now or time.time()
        usd = min(usd, self.cash)
        qty = usd * (1 - self.fee) / price
        pos = Position(mint, symbol, mood, price, qty, usd, now, price, now)
        self.cash -= usd
        self.positions[mint] = pos
        self.buy_times.append(now)
        self._journal({
            "time": _iso(now), "action": "BUY", "mint": mint, "symbol": symbol, "mood": mood,
            "price": f"{price:.12g}", "usd": f"{usd:.2f}", "pnl_usd": "", "pnl_pct": "", "reason": "mood",
        })
        self.save(now)
        return pos

    def sell(self, mint: str, price: float, reason: str, cooldown_s: float, now: float | None = None) -> tuple[float, float]:
        now = now or time.time()
        pos = self.positions.pop(mint)
        value = pos.qty * price * (1 - self.fee)
        pnl = value - pos.cost_usd
        pct = pnl / pos.cost_usd * 100 if pos.cost_usd else 0.0
        self.cash += value
        self.cooldowns[mint] = now + cooldown_s
        self._journal({
            "time": _iso(now), "action": "SELL", "mint": mint, "symbol": pos.symbol, "mood": pos.mood,
            "price": f"{price:.12g}", "usd": f"{value:.2f}", "pnl_usd": f"{pnl:.2f}",
            "pnl_pct": f"{pct:.2f}", "reason": reason,
        })
        self.save(now)
        return pnl, pct

    def equity(self) -> float:
        return self.cash + sum(p.qty * (p.last_price or p.entry_price) * (1 - self.fee) for p in self.positions.values())


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) + "Z"
