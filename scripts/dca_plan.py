"""Print the DCA ladder for the current config: prices, sizes, average, TP, liquidation.

    python scripts/dca_plan.py              # uses entry price 100 (percentages are what matter)
    python scripts/dca_plan.py --price 150  # show real prices for SOL at $150
"""
from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dca.config import load_dca_settings  # noqa: E402
from dca.ladder import build_plan  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--price", type=float, default=100.0)
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    s = load_dca_settings(args.config)
    total_margin = 0.0
    for side in s.sides:
        plan = build_plan(args.price, side, s.ladder.levels(), s.ladder.take_profit_pct,
                          s.ladder.stop_loss_pct, s.leverage, s.maintenance_margin_rate)
        total_margin += plan.margin_usdt
        print(f"\n=== {side.upper()} @ {args.price} | leverage {s.leverage}x isolated ===")
        print(f"{'order':>6} {'dev%':>7} {'price':>10} {'USDT':>8} {'cum USDT':>9} "
              f"{'avg':>10} {'TP':>10} {'liq':>10}")
        for r in plan.rows:
            name = "base" if r.index == 0 else f"SO{r.index}"
            print(f"{name:>6} {r.deviation_pct:>7.2f} {r.price:>10.4f} {r.notional_usdt:>8.2f} "
                  f"{r.cum_notional_usdt:>9.2f} {r.avg_price:>10.4f} {r.tp_price:>10.4f} {r.liq_price:>10.4f}")
        loss = abs(plan.rows[-1].avg_price - plan.sl_price) * sum(r.qty for r in plan.rows)
        print(f"stop loss {plan.sl_price:.4f} ({s.ladder.stop_loss_pct}% from entry) | "
              f"max loss ≈ ${loss:.2f} | margin ${plan.margin_usdt:.2f} | "
              f"min liquidation buffer {plan.min_liq_buffer_pct:.2f}%")
    print(f"\nWorst-case margin (all sides fully filled): ${total_margin:.2f} of "
          f"${s.capital_usdt:.0f} capital ({total_margin / s.capital_usdt:.0%})")


if __name__ == "__main__":
    main()
