"""Research report for the Up/Down engine: is the model actually smart?

    python scripts/updown_report.py                 # reads data/
    python scripts/updown_report.py --data-dir data/sim

Reads the journals the engine writes and prints:
  1. P&L per strategy (booked settlements).
  2. Calibration: Brier score of the model vs the market price, bucketed by
     time remaining. If the model doesn't beat the market here, it has no
     edge -- no matter what the P&L of the last few days says.
  3. Reliability table: predicted probability vs how often Up really won.
  4. Settlement-rule check: how often the TWAP rule vs the last-price rule
     agrees with Polymarket's official result. If "last" matches better,
     set settlement.rule: last.
"""
from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bot.updown.analysis import calibration, load_settlements, pnl_by_strategy, rule_check  # noqa: E402

VERDICT = {"model": "model better", "market": "market better", "tie": "tie"}


def pnl_section(rows: list) -> list[str]:
    per = pnl_by_strategy(rows)
    lines = ["1) P&L per strategy (booked settlements)"]
    if not per:
        return lines + ["   no settled positions yet"]
    total = 0.0
    for strat, s in sorted(per.items()):
        total += s["pnl"]
        lines.append(
            f"   {strat:<17} windows={s['windows']:<5} win_rate={s['wins'] / s['windows']:>5.0%} "
            f"pnl=${s['pnl']:+9.2f}  avg=${s['pnl'] / s['windows']:+.3f}  worst=${s['worst']:+.2f}"
        )
    lines.append(f"   {'TOTAL':<17} pnl=${total:+.2f}")
    return lines


def calibration_sections(snapshots_path: str, outcomes: dict) -> list[str]:
    cal = calibration(snapshots_path, outcomes)
    lines = ["", "2) Calibration: Brier score, model vs market mid (lower is better)"]
    if cal["n"] == 0:
        return lines + ["   no snapshots with known outcomes yet"]
    lines.append(f"   {'remaining':<12}{'n':>7}{'model':>10}{'market':>10}   verdict")
    for b in cal["buckets"]:
        lines.append(f"   {b['label']:<12}{b['n']:>7}{b['model']:>10.4f}{b['market']:>10.4f}   {VERDICT[b['verdict']]}")
    lines += ["", "3) Reliability: predicted P(Up) vs realized Up frequency"]
    lines.append(f"   {'bin':<10}{'n':>7}{'avg p':>9}{'realized':>10}")
    for r in cal["reliability"]:
        lines.append(f"   {r['lo']:.1f}-{r['hi']:.1f}  {r['n']:>7}{r['avg_p']:>9.3f}{r['realized']:>10.3f}")
    return lines


def rule_section(rows: list) -> list[str]:
    check = rule_check(rows)
    lines = ["", "4) Settlement rule check vs official results"]
    if not check["windows"]:
        return lines + ["   no official resolutions recorded yet"]
    for rule in ("twap", "last"):
        ok, n = check[rule]["ok"], check[rule]["n"]
        if n:
            lines.append(f"   {rule:<5} rule matched {ok}/{n} ({ok / n:.1%})")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()
    outcomes, rows = load_settlements(os.path.join(args.data_dir, "updown_settlements.csv"))
    lines = [f"Up/Down report for {args.data_dir}/ ({len(outcomes)} windows with outcomes)", ""]
    lines += pnl_section(rows)
    lines += calibration_sections(os.path.join(args.data_dir, "updown_snapshots.jsonl"), outcomes)
    lines += rule_section(rows)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
