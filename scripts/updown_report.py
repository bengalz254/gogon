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
import csv
import json
import os
import sys
from collections import defaultdict

BUCKETS = [(0, 30), (30, 60), (60, 120), (120, 180), (180, 10**9)]


def load_outcomes(path: str) -> tuple[dict, list]:
    """window_id -> winner (official preferred), plus the raw rows."""
    outcomes, rows = {}, []
    if not os.path.exists(path):
        return outcomes, rows
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
            wid = row["window_id"]
            winner = row.get("official_winner") or row.get("booked_winner") or row.get("provisional_winner")
            if row.get("official_winner"):
                outcomes[wid] = row["official_winner"]
            elif winner and wid not in outcomes:
                outcomes[wid] = winner
    return outcomes, rows


def pnl_section(rows: list) -> list[str]:
    per = defaultdict(lambda: {"windows": 0, "wins": 0, "pnl": 0.0, "worst": 0.0})
    seen = {}
    for row in rows:
        if row.get("event") not in ("booked", "official"):
            continue
        try:
            by = json.loads(row.get("pnl_by_strategy") or "{}")
        except ValueError:
            continue
        if by:
            seen[row["window_id"]] = by  # last row per window wins (includes corrections)
    for by in seen.values():
        for strat, p in by.items():
            s = per[strat]
            s["windows"] += 1
            s["wins"] += p > 0
            s["pnl"] += p
            s["worst"] = min(s["worst"], p)
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
    stats = {b: {"n": 0, "bm": 0.0, "bk": 0.0} for b in BUCKETS}
    rel = defaultdict(lambda: [0, 0.0, 0])  # bin -> [n, sum_p, ups]
    n_total = 0
    if os.path.exists(snapshots_path):
        with open(snapshots_path, encoding="utf-8") as f:
            for line in f:
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                winner = outcomes.get(s.get("window_id"))
                if winner not in ("Up", "Down"):
                    continue
                y = 1.0 if winner == "Up" else 0.0
                p = s.get("p_up")
                bid, ask = s.get("up_bid"), s.get("up_ask")
                if p is None or bid is None or ask is None:
                    continue
                mkt = (bid + ask) / 2.0
                rem = s.get("remaining", 0)
                for b in BUCKETS:
                    if b[0] <= rem < b[1]:
                        st = stats[b]
                        st["n"] += 1
                        st["bm"] += (p - y) ** 2
                        st["bk"] += (mkt - y) ** 2
                bin_i = min(9, int(p * 10))
                rel[bin_i][0] += 1
                rel[bin_i][1] += p
                rel[bin_i][2] += y
                n_total += 1

    lines = ["", "2) Calibration: Brier score, model vs market mid (lower is better)"]
    if n_total == 0:
        return lines + ["   no snapshots with known outcomes yet"]
    lines.append(f"   {'remaining':<12}{'n':>7}{'model':>10}{'market':>10}   verdict")
    for b in BUCKETS:
        st = stats[b]
        if not st["n"]:
            continue
        bm, bk = st["bm"] / st["n"], st["bk"] / st["n"]
        label = f"{b[0]}-{b[1]}s" if b[1] < 10**9 else f">={b[0]}s"
        verdict = "model better" if bm < bk - 1e-4 else ("market better" if bk < bm - 1e-4 else "tie")
        lines.append(f"   {label:<12}{st['n']:>7}{bm:>10.4f}{bk:>10.4f}   {verdict}")
    lines += ["", "3) Reliability: predicted P(Up) vs realized Up frequency"]
    lines.append(f"   {'bin':<10}{'n':>7}{'avg p':>9}{'realized':>10}")
    for i in range(10):
        n, sp, ups = rel.get(i, [0, 0.0, 0])
        if n:
            lines.append(f"   {i / 10:.1f}-{(i + 1) / 10:.1f}  {n:>7}{sp / n:>9.3f}{ups / n:>10.3f}")
    return lines


def rule_section(rows: list) -> list[str]:
    agree = {"twap": [0, 0], "last": [0, 0]}
    seen = set()
    for row in rows:
        official = row.get("official_winner")
        if not official or row["window_id"] in seen:
            continue
        seen.add(row["window_id"])
        for rule, key in (("twap", "winner_twap_rule"), ("last", "winner_last_rule")):
            if row.get(key):
                agree[rule][1] += 1
                agree[rule][0] += row[key] == official
    lines = ["", "4) Settlement rule check vs official results"]
    if not seen:
        return lines + ["   no official resolutions recorded yet"]
    for rule, (ok, n) in agree.items():
        if n:
            lines.append(f"   {rule:<5} rule matched {ok}/{n} ({ok / n:.1%})")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()
    outcomes, rows = load_outcomes(os.path.join(args.data_dir, "updown_settlements.csv"))
    lines = [f"Up/Down report for {args.data_dir}/ ({len(outcomes)} windows with outcomes)", ""]
    lines += pnl_section(rows)
    lines += calibration_sections(os.path.join(args.data_dir, "updown_snapshots.jsonl"), outcomes)
    lines += rule_section(rows)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
