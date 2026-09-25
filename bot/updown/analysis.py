"""Read-only analysis of the Up/Down journals, shared by
scripts/updown_report.py (text) and scripts/updown_dashboard.py (JSON)."""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from datetime import datetime

BUCKETS = [(0, 30), (30, 60), (60, 120), (120, 180), (180, 10**9)]


def load_settlements(path: str) -> tuple[dict, list]:
    """(window_id -> winner, official preferred; raw settlement rows)."""
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


def _iso_ts(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def booked_windows(rows: list) -> dict:
    """window_id -> latest booked P&L row (corrections included)."""
    seen = {}
    for row in rows:
        if row.get("event") not in ("booked", "official"):
            continue
        try:
            by = json.loads(row.get("pnl_by_strategy") or "{}")
        except ValueError:
            continue
        if by:
            seen[row["window_id"]] = {
                "window_id": row["window_id"], "asset": row.get("asset", ""),
                "start": _iso_ts(row.get("start", "")), "end": _iso_ts(row.get("end", "")),
                "by_strategy": by,
                "winner": row.get("official_winner") or row.get("booked_winner") or "",
            }
    return seen


def pnl_by_strategy(rows: list) -> dict:
    per = defaultdict(lambda: {"windows": 0, "wins": 0, "pnl": 0.0, "worst": 0.0})
    for w in booked_windows(rows).values():
        for strat, p in w["by_strategy"].items():
            s = per[strat]
            s["windows"] += 1
            s["wins"] += p > 0
            s["pnl"] += p
            s["worst"] = min(s["worst"], p)
    return dict(per)


def pnl_timeline(rows: list) -> list[dict]:
    """Booked windows in settlement order with the running P&L total."""
    points = sorted(
        (w for w in booked_windows(rows).values() if w["end"] is not None), key=lambda w: w["end"]
    )
    cum, out = 0.0, []
    for w in points:
        pnl = sum(w["by_strategy"].values())
        cum += pnl
        out.append({"start": w["start"], "end": w["end"], "window_id": w["window_id"],
                    "asset": w["asset"], "winner": w["winner"], "pnl": pnl, "cum": cum})
    return out


def calibration(snapshots_path: str, outcomes: dict) -> dict:
    """Brier score of model vs market mid by time remaining, plus reliability bins."""
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
    buckets = []
    for b in BUCKETS:
        st = stats[b]
        if not st["n"]:
            continue
        bm, bk = st["bm"] / st["n"], st["bk"] / st["n"]
        verdict = "model" if bm < bk - 1e-4 else ("market" if bk < bm - 1e-4 else "tie")
        buckets.append({
            "label": f"{b[0]}-{b[1]}s" if b[1] < 10**9 else f">={b[0]}s",
            "n": st["n"], "model": bm, "market": bk, "verdict": verdict,
        })
    reliability = [
        {"lo": i / 10, "hi": (i + 1) / 10, "n": n, "avg_p": sp / n, "realized": ups / n}
        for i in range(10)
        for n, sp, ups in [rel.get(i, [0, 0.0, 0])]
        if n
    ]
    return {"n": n_total, "buckets": buckets, "reliability": reliability}


def rule_check(rows: list) -> dict:
    """How often each settlement rule (with its own strike) matched the official result."""
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
    return {"windows": len(seen), **{rule: {"ok": ok, "n": n} for rule, (ok, n) in agree.items()}}


def tail_csv(path: str, limit: int, max_bytes: int = 400_000) -> list[dict]:
    """Last `limit` rows of a CSV file without reading all of it."""
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        header = f.readline().decode("utf-8", "replace")
        size = f.seek(0, os.SEEK_END)
        f.seek(max(len(header), size - max_bytes))
        chunk = f.read().decode("utf-8", "replace")
    lines = chunk.splitlines()
    if size - max_bytes > len(header):
        lines = lines[1:]  # first line is probably cut in half
    rows = list(csv.DictReader([header.strip()] + lines))
    return rows[-limit:]
