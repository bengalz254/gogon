"""Compare DCA config variants on the same candles.

    python scripts/dca_sweep.py --csv data/candles_SOLUSDT_15m_180d.csv

Each variant starts from config/dca.yaml and overrides a few fields.
Sorted by NET PnL (realized + unrealized at the end).
"""
from __future__ import annotations

import argparse
import copy
import logging
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dca.config import load_dca_settings  # noqa: E402
from scripts.dca_backtest import load_csv, run_backtest  # noqa: E402

# name -> {"section.field" or "field": value}
VARIANTS: dict[str, dict] = {
    "default": {},
    "long only": {"sides": ["long"]},
    "short only": {"sides": ["short"]},
    "trend EMA200": {"entry.trend_filter": True, "entry.ema_period": 200},
    "trend EMA800": {"entry.trend_filter": True, "entry.ema_period": 800},
    "TP 1.5%": {"ladder.take_profit_pct": 1.5},
    "TP 2%": {"ladder.take_profit_pct": 2.0},
    "wide ladder (2% step, SL 30)": {"ladder.price_deviation_pct": 2.0, "ladder.stop_loss_pct": 30},
    "SL 35": {"ladder.stop_loss_pct": 35},
    "RSI 25/75": {"entry.long_rsi_below": 25, "entry.short_rsi_above": 75},
    "trend EMA800 + TP 1.5%": {"entry.trend_filter": True, "entry.ema_period": 800,
                               "ladder.take_profit_pct": 1.5},
    "trend EMA800 + wide ladder": {"entry.trend_filter": True, "entry.ema_period": 800,
                                   "ladder.price_deviation_pct": 2.0, "ladder.stop_loss_pct": 30},
    "long only + trend EMA800": {"sides": ["long"], "entry.trend_filter": True, "entry.ema_period": 800},
}


def apply(base, overrides: dict):
    s = copy.deepcopy(base)
    for key, value in overrides.items():
        target = s
        *path, field = key.split(".")
        for part in path:
            target = getattr(target, part)
        setattr(target, field, value)
    s.validate()
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="candles CSV saved by dca_backtest.py")
    ap.add_argument("--config")
    args = ap.parse_args()

    logging.getLogger("dcabot").setLevel(logging.WARNING)
    base = load_dca_settings(args.config)
    candles = load_csv(args.csv)
    if len(candles) < 50:
        sys.exit("not enough candles")

    results = []
    for name, overrides in VARIANTS.items():
        started = time.time()
        try:
            s = apply(base, overrides)
        except ValueError as e:
            print(f"skip {name}: {e}")
            continue
        r = run_backtest(s, candles)
        results.append((name, r))
        print(f"done {name:<30} ({time.time() - started:.0f}s)", flush=True)

    r0 = results[0][1]
    print(f"\n{r0['from']} -> {r0['to']} | {r0['candles']} candles | price {r0['price_change_pct']:+.1f}%\n")
    print(f"{'variant':<30} {'deals':>5} {'stops':>5} {'realized':>9} {'open uPnL':>9} "
          f"{'NET':>9} {'maxDD':>8} {'maxSO':>5}")
    for name, r in sorted(results, key=lambda x: x[1]["net_pnl"], reverse=True):
        stops = r["closed_deals"] - r["by_reason"].get("take_profit", 0)
        print(f"{name:<30} {r['closed_deals']:>5} {stops:>5} {r['realized_pnl']:>9.2f} "
              f"{r['unrealized_pnl']:>9.2f} {r['net_pnl']:>9.2f} {r['max_drawdown']:>8.2f} "
              f"{r['max_safety_orders_used']:>5}")


if __name__ == "__main__":
    main()
