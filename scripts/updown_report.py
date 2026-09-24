"""One-screen health report for the 5-minute Up/Down bot.

    python scripts/updown_report.py            # everything in the journal
    python scripts/updown_report.py --hours 24 # last 24 hours only

Reads data/trades.csv (trades) and logs/bot.log (connection problems).
Paste the output when asking for a check-up.
"""
from __future__ import annotations

import argparse
import csv
import os
import re

FAIR_RX = re.compile(r"fair ([0-9.]+)")
from collections import defaultdict
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES = os.path.join(_ROOT, "data", "trades.csv")
LOG = os.path.join(_ROOT, "logs", "bot.log")

LOG_PROBLEMS = {
    "feed failures": re.compile(r"price poll failed"),
    "stale feed": re.compile(r"price feed stale"),
    "skipped windows": re.compile(r"skipping window"),
    "market not found": re.compile(r"no market found"),
    "order-book failures": re.compile(r"order-book fetch failed|Order book fetch failed"),
    "blocked by Windows/VPN (10013)": re.compile(r"WinError 10013"),
}


def window_results(rows: list[dict]) -> dict[str, dict]:
    """Per window (slug): cash in/out, whether it has settled, coin."""
    out: dict[str, dict] = {}
    for r in rows:
        if r.get("strategy") != "updown_5m" or r.get("filled", "").lower() != "true":
            continue
        slug = r.get("group_id") or r.get("market_id")
        w = out.setdefault(slug, {"coin": slug.split("-", 1)[0].upper(), "cash": 0.0, "bought": 0.0, "shares": 0.0, "settled": False, "expected": 0.0})
        usd, shares = float(r["size_usd"]), float(r["size_shares"])
        if r["side"] == "BUY":
            w["cash"] -= usd
            w["bought"] += usd
            w["shares"] += shares
            m = FAIR_RX.search(r.get("reason", ""))
            if m:  # what the model said these shares were worth, minus what they cost
                w["expected"] += shares * float(m.group(1)) - usd
        else:
            w["cash"] += usd
            w["shares"] -= shares
        if r.get("reason", "").startswith("settled"):
            w["settled"] = True
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, help="only look at the last N hours")
    args = ap.parse_args()
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours) if args.hours else None

    rows = []
    if os.path.exists(TRADES):
        with open(TRADES, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if not since or datetime.fromisoformat(r["timestamp"]) >= since]
    mode = sorted({r["mode"] for r in rows if r.get("strategy") == "updown_5m"}) or ["-"]
    windows = window_results(rows)
    # A window with shares still open hasn't finished: leave it out of P&L.
    done = {k: w for k, w in windows.items() if w["settled"] or w["shares"] <= 1e-6}
    open_w = len(windows) - len(done)

    print(f"=== GOGON UP/DOWN REPORT ({', '.join(mode)}) {'last %gh' % args.hours if args.hours else 'all time'} ===")
    per = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "staked": 0.0, "exp": 0.0})
    for w in done.values():
        p = per[w["coin"]]
        p["n"] += 1
        p["pnl"] += w["cash"]
        p["staked"] += w["bought"]
        p["exp"] += w["expected"]
        if w["cash"] > 0:
            p["w"] += 1
        elif w["cash"] < 0:
            p["l"] += 1
    print(f"{'COIN':6}{'WINDOWS':>9}{'WIN':>6}{'LOSS':>6}{'WIN%':>7}{'P&L':>11}{'EXPECTED':>10}{'STAKED':>10}{'ROI':>8}")
    tot = {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "staked": 0.0, "exp": 0.0}
    for coin in sorted(per):
        p = per[coin]
        for k in tot:
            tot[k] += p[k]
        wr = p["w"] / (p["w"] + p["l"]) * 100 if p["w"] + p["l"] else 0
        roi = p["pnl"] / p["staked"] * 100 if p["staked"] else 0
        print(f"{coin:6}{p['n']:>9}{p['w']:>6}{p['l']:>6}{wr:>6.0f}%{p['pnl']:>+11.2f}{p['exp']:>+10.2f}{p['staked']:>10.2f}{roi:>+7.1f}%")
    wr = tot["w"] / (tot["w"] + tot["l"]) * 100 if tot["w"] + tot["l"] else 0
    roi = tot["pnl"] / tot["staked"] * 100 if tot["staked"] else 0
    print(f"{'TOTAL':6}{tot['n']:>9}{tot['w']:>6}{tot['l']:>6}{wr:>6.0f}%{tot['pnl']:>+11.2f}{tot['exp']:>+10.2f}{tot['staked']:>10.2f}{roi:>+7.1f}%")
    print(f"open (not yet settled) windows: {open_w}")
    if tot["n"] >= 20 and tot["exp"] > 0:
        ratio = tot["pnl"] / tot["exp"]
        verdict = ("model's edges look real" if ratio > 0.6 else
                   "edges partly real; model is too optimistic" if ratio > 0.1 else
                   "edges are NOT showing up: the market likely knows something our data doesn't")
        print(f"realised / expected P&L: {ratio:+.0%}  -> {verdict}")

    if os.path.exists(LOG):
        counts = defaultdict(int)
        last_line = ""
        with open(LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                if since:
                    try:
                        ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    if ts < since - timedelta(hours=14):  # log uses local time; be generous
                        continue
                for name, rx in LOG_PROBLEMS.items():
                    if rx.search(line):
                        counts[name] += 1
                last_line = line.strip()
        print("\nlog problems: " + (", ".join(f"{k} {v}" for k, v in counts.items() if v) or "none"))
        print("last log line: " + last_line[:160])


if __name__ == "__main__":
    main()
