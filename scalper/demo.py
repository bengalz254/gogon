"""Demo data for previewing the dashboard before the bot has traded.

    python -m scalper dashboard --demo

The trades are produced by the bot's REAL strategy, risk and trade-management
code (through the backtester), but on SYNTHETIC prices: a pure random walk.
On a random walk no strategy can have an edge, so the demo P&L is just noise
minus fees — it shows what the dashboard looks like, not how the bot will
perform. The seeds were picked to give a typical (median) outcome across many
random runs, not a flattering one.

Everything is written to separate trades_demo.csv / state_demo.json files, so
it never mixes with paper, testnet or live results, and Binance is never
contacted.
"""
from __future__ import annotations

import csv
import math
import os
import random
from dataclasses import replace
from datetime import datetime, timezone

from scalper.backtest import ClosedTrade, run_backtest
from scalper.config import Settings
from scalper.journal import FIELDS, StateStore, utc
from scalper.models import Candle

DEMO_MODE = "demo"
# symbol -> (starting price, volatility per 5m bar, random seed)
DEMO_MARKETS = {
    "BTCUSDT": (60_000.0, 0.0018, 611),
    "ETHUSDT": (2_500.0, 0.0024, 607),
    "SOLUSDT": (150.0, 0.0032, 605),
}


def synthetic_candles(p0: float, vol: float, seed: int, bars: int, step_ms: int, end_ms: int) -> list[Candle]:
    """Driftless random walk with realistic wicks (no hidden trend to exploit)."""
    rnd = random.Random(seed)
    out: list[Candle] = []
    price = p0
    start = end_ms - bars * step_ms
    for i in range(bars):
        path = [price]
        for _ in range(5):
            path.append(path[-1] * (1 + rnd.gauss(0.0, vol / math.sqrt(5))))
        high = max(path) * (1 + abs(rnd.gauss(0, vol * 0.15)))
        low = min(path) * (1 - abs(rnd.gauss(0, vol * 0.15)))
        t = start + i * step_ms
        out.append(Candle(t, path[0], high, low, path[-1], rnd.uniform(50, 150), t + step_ms - 1))
        price = path[-1]
    return out


def _journal_row(t: ClosedTrade, equity_after: float) -> dict:
    return {
        "closed_at_utc": utc(t.exit_time),
        "mode": DEMO_MODE,
        "trade_id": t.trade_id,
        "symbol": t.symbol,
        "side": t.side,
        "strategy": t.strategy,
        "entry_time_utc": utc(t.entry_time),
        "exit_time_utc": utc(t.exit_time),
        "entry_price": f"{t.entry_price:.8g}",
        "exit_price": f"{t.exit_price:.8g}",
        "qty": f"{t.qty:.8g}",
        "initial_stop": f"{t.initial_stop:.8g}",
        "take_profit": f"{t.take_profit:.8g}",
        "exit_reason": t.exit_reason,
        "gross_pnl": f"{t.gross_pnl:.6f}",
        "fees": f"{t.fees:.6f}",
        "funding": f"{t.funding:.6f}",
        "net_pnl": f"{t.net_pnl:.6f}",
        "r_multiple": f"{t.r_multiple:.3f}",
        "bars_held": t.bars_held,
        "equity_after": f"{equity_after:.4f}",
        "approximate": False,
        "note": "DEMO: synthetic prices",
    }


def write_demo_data(settings: Settings, now_ms: int, days: float = 21.0) -> dict[str, str]:
    """Generate the demo journal + state. Returns their paths."""
    base = replace(settings, mode=DEMO_MODE)
    step = base.interval_ms
    end = now_ms - now_ms % step
    bars = int(days * 86_400_000 // step)
    balance = base.paper.starting_balance

    closed: list[ClosedTrade] = []
    still_open: dict[str, ClosedTrade] = {}
    for sym, (p0, vol, seed) in DEMO_MARKETS.items():
        candles = synthetic_candles(p0, vol, seed, bars, step, end)
        result = run_backtest(candles, base, sym, starting_equity=balance)
        for t in result.trades:
            if t.exit_reason == "END":
                still_open[sym] = t  # the backtest ended with this trade running
            else:
                closed.append(t)
    closed.sort(key=lambda t: t.exit_time)

    os.makedirs(base.data_dir, exist_ok=True)
    journal_path = os.path.join(base.data_dir, f"trades_{DEMO_MODE}.csv")
    state_path = os.path.join(base.data_dir, f"state_{DEMO_MODE}.json")

    equity = balance
    with open(journal_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for t in closed:
            equity += t.net_pnl
            writer.writerow(_journal_row(t, equity))

    today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    todays = [t for t in closed if utc(t.exit_time).startswith(today)]
    state = {
        "version": 1,
        "mode": DEMO_MODE,
        "saved_at": utc(now_ms),
        "risk": {
            "day": today,
            "realized_today": sum(t.net_pnl for t in todays),
            "trades_today": len(todays),
            "halted": False,
            "halt_reason": "",
        },
        "trades": {
            sym: {
                "symbol": sym,
                "side": t.side,
                "qty": t.qty,
                "entry_price": t.entry_price,
                "stop": t.initial_stop,
                "stop_kind": "SL",
                "take_profit": t.take_profit,
                "bars_held": t.bars_held,
            }
            for sym, t in still_open.items()
        },
        "paper": {"balance": equity},
    }
    StateStore(state_path).save(state)
    return {"journal": journal_path, "state": state_path}
