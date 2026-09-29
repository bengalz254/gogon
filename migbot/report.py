"""Research and P&L summary from data/migbot/tokens.csv and trades.csv.

The central question: do the tokens the filters bought do better than the
migrated tokens they rejected? Returns are measured from the reference
price (the first moment the bot could have bought), so both groups are
compared from the same starting point.
"""
from __future__ import annotations

import os
import statistics
from collections import Counter

from migbot.storage import read_csv
from migbot.tracker import BOUGHT, NO_DATA, PASSED, REJECTED

MIN_SAMPLE = 30
DIAG_MIN = 5  # positions needed before the entry-or-exit hint is shown
NEVER_UP_PCT = 10  # "never rose": the best price while held stayed under +10%
GAVE_BACK_PCT = 20  # "gave back a gain": was +20% or more, closed at a loss
RECENT_ROWS = 12


def _f(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _group_stats(rows: list[dict], columns: list[str]) -> dict:
    out = {"n": len(rows), "columns": {}}
    for col in columns:
        values = [v for v in (_f(r.get(col)) for r in rows) if v is not None]
        if not values:
            out["columns"][col] = {"n": 0}
            continue
        out["columns"][col] = {
            "n": len(values),
            "median": statistics.median(values),
            "mean": statistics.fmean(values),
            "pct_up": sum(v > 0 for v in values) / len(values) * 100,
        }
    maxes = [v for v in (_f(r.get("max_ret_pct")) for r in rows) if v is not None]
    mins = [v for v in (_f(r.get("min_ret_pct")) for r in rows) if v is not None]
    out["pct_2x"] = sum(v >= 100 for v in maxes) / len(maxes) * 100 if maxes else None
    out["pct_halved"] = sum(v <= -50 for v in mins) / len(mins) * 100 if mins else None
    return out


def summarize(data_dir: str) -> dict:
    tokens = read_csv(os.path.join(data_dir, "tokens.csv"))
    trades = read_csv(os.path.join(data_dir, "trades.csv"))
    ret_cols = [c for c in (tokens[0].keys() if tokens else []) if c.startswith("ret_") and c.endswith("m")]

    groups = {
        "semua": [t for t in tokens if t.get("status") != NO_DATA],
        "lolos filter": [t for t in tokens if t.get("status") in (BOUGHT, PASSED)],
        "dibeli": [t for t in tokens if t.get("status") == BOUGHT],
        "ditolak": [t for t in tokens if t.get("status") == REJECTED],
    }
    research = {name: _group_stats(rows, ret_cols) for name, rows in groups.items()}

    reasons = Counter()
    for t in groups["ditolak"]:
        for reason in (t.get("reasons") or "").split(" | "):
            if reason:
                reasons[reason.split(":")[0].strip()] += 1
    dangers = Counter()
    for t in tokens:
        for risk in (t.get("rugcheck_danger") or "").split("; "):
            if risk:
                dangers[risk] += 1

    sells = [t for t in trades if t.get("side") == "SELL"]
    buys = [t for t in trades if t.get("side") == "BUY"]
    closes = [t for t in sells if t.get("position_pnl_sol")]
    pnls = [_f(t.get("position_pnl_sol")) or 0.0 for t in closes]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    timeline, cum = [], 0.0
    for t in sells:
        cum += _f(t.get("pnl_sol")) or 0.0
        timeline.append({"t": t.get("time_utc", ""), "pnl": round(cum, 6)})
    fees = sum(_f(t.get("fee_sol")) or 0.0 for t in trades)
    exits = Counter(t.get("reason") or "?" for t in sells)
    positions = _positions(buys, closes)
    pnl = {
        "buys": len(buys),
        "closed": len(closes),
        "wins": len(wins),
        "win_rate": len(wins) / len(closes) * 100 if closes else None,
        "realized_sol": sum(_f(t.get("pnl_sol")) or 0.0 for t in sells),
        "avg_win_sol": statistics.fmean(wins) if wins else None,
        "avg_loss_sol": statistics.fmean(losses) if losses else None,
        "best_sol": max(pnls) if pnls else None,
        "worst_sol": min(pnls) if pnls else None,
        "fees_sol": fees,
        "exit_reasons": dict(exits.most_common()),
        "methods": dict(Counter(t.get("method", "?") for t in trades).most_common()),
        "timeline": timeline[-500:],
        "dip_mode": any((t.get("reason") or "").startswith("beli saat dip") for t in buys),
        "diagnosis": _diagnosis(positions),
        "recent": positions[-RECENT_ROWS:],
    }
    return {
        "tokens_total": len(tokens),
        "no_data": sum(t.get("status") == NO_DATA for t in tokens),
        "ret_columns": ret_cols,
        "research": research,
        "reject_reasons": dict(reasons.most_common(12)),
        "rugcheck_dangers": dict(dangers.most_common(8)),
        "pnl": pnl,
        "verdict": verdict(research, pnl),
    }


def _positions(buys: list[dict], closes: list[dict]) -> list[dict]:
    """One row per closed position: final exit, whole-position P&L %, and how the price moved while held."""
    buy_by_mint = {b.get("mint"): b for b in buys}
    rows = []
    for t in closes:
        cost = _f((buy_by_mint.get(t.get("mint")) or {}).get("sol"))
        pnl_sol = _f(t.get("position_pnl_sol"))
        rows.append({
            "time": t.get("time_utc", ""),
            "symbol": t.get("symbol", ""),
            "reason": t.get("reason", ""),
            "pnl_sol": pnl_sol,
            "pnl_pct": pnl_sol / cost * 100 if cost and pnl_sol is not None else None,
            "peak_pct": _f(t.get("peak_pct")),
            "low_pct": _f(t.get("low_pct")),
            "held_min": _f(t.get("held_min")),
            "entry_age_min": _f(t.get("entry_age_min")),
        })
    return rows


def _diagnosis(positions: list[dict]) -> dict:
    """Is the loss in the entry (price never rises after buying) or the exit (a gain given back)?"""
    known = [p for p in positions if p["peak_pct"] is not None]
    ages = [p["entry_age_min"] for p in known if p["entry_age_min"] is not None]
    held = [p["held_min"] for p in known if p["held_min"] is not None]
    return {
        "n": len(known),
        "never_up": sum(p["peak_pct"] < NEVER_UP_PCT for p in known),
        "gave_back": sum(p["peak_pct"] >= GAVE_BACK_PCT and (p["pnl_sol"] or 0.0) <= 0 for p in known),
        "median_entry_age_min": statistics.median(ages) if ages else None,
        "median_held_min": statistics.median(held) if held else None,
    }


def _diagnosis_hint(d: dict) -> str | None:
    if d["n"] < DIAG_MIN:
        return None
    never_up, gave_back = d["never_up"] / d["n"], d["gave_back"] / d["n"]
    if never_up >= 0.5:
        return (f"{d['never_up']} dari {d['n']} posisi tidak pernah naik {NEVER_UP_PCT}% setelah dibeli: "
                "masalah utamanya WAKTU BELI (bot membeli lalu harga langsung turun).")
    if gave_back >= 0.3:
        return (f"{d['gave_back']} dari {d['n']} posisi sempat naik {GAVE_BACK_PCT}%+ tapi ditutup rugi: "
                "masalah utamanya CARA JUAL (untung tidak diamankan).")
    return None


def verdict(research: dict, pnl: dict) -> list[str]:
    lines = []
    if pnl["closed"] < MIN_SAMPLE:
        lines.append(
            f"Baru {pnl['closed']} posisi selesai. Butuh minimal {MIN_SAMPLE} (lebih baik 100+) sebelum hasil ini berarti; "
            "sampai itu, untung/rugi masih bisa kebetulan."
        )
    col = next((c for c in ("ret_60m", "ret_30m", "ret_15m") if c in research["lolos filter"]["columns"]), None)
    if pnl.get("dip_mode"):
        # Tokens that pass in dip mode have, by definition, fallen after the reference point,
        # so comparing groups from that point says nothing about the filters. Judge by P&L.
        col = None
        lines.append("Mode beli saat dip: nilai dari P&L dan diagnosa posisi; tabel riset diukur dari titik yang sama untuk semua token, bukan dari titik beli.")
    if col:
        b = research["lolos filter"]["columns"][col]
        r = research["ditolak"]["columns"][col]
        if b.get("n") and r.get("n"):
            better = b["median"] > r["median"]
            lines.append(
                f"Median {col[4:]} setelah titik pembanding: lolos filter {b['median']:+.1f}% vs ditolak {r['median']:+.1f}%. "
                + ("Filter memilih token yang lebih baik dari yang ditolak." if better else "Filter BELUM terbukti lebih baik dari token yang ditolak.")
            )
    if pnl["closed"]:
        lines.append(
            f"P&L paper {pnl['realized_sol']:+.4f} SOL dari {pnl['closed']} posisi (win rate {pnl['win_rate']:.0f}%), "
            f"sudah termasuk fee {pnl['fees_sol']:.4f} SOL."
        )
    hint = _diagnosis_hint(pnl.get("diagnosis") or {"n": 0})
    if hint:
        lines.append(hint)
    if pnl["closed"] >= MIN_SAMPLE and pnl["realized_sol"] <= 0:
        lines.append("Dengan pengaturan ini bot rugi. Jangan dipakai dengan uang sungguhan.")
    return lines


def _fmt(value, suffix: str = "%", digits: int = 1) -> str:
    return "-" if value is None else f"{value:+.{digits}f}{suffix}"


def format_report(summary: dict) -> str:
    lines = ["=" * 64, " Laporan bot meme coin migrated (paper)", "=" * 64]
    lines.append(f"Token selesai dipantau: {summary['tokens_total']} (tanpa data pasar: {summary['no_data']})")
    cols = summary["ret_columns"]
    if cols:
        lines.append("")
        lines.append("Kenaikan harga dari titik pembanding, awal jendela beli (median, dan % token yang naik):")
        header = f"{'kelompok':<13}{'n':>5} " + "".join(f"{c[4:]:>13}" for c in cols) + f"{'pernah 2x':>11}{'pernah -50%':>13}"
        lines.append(header)
        for name, g in summary["research"].items():
            cells = []
            for c in cols:
                st = g["columns"].get(c, {})
                cells.append(f"{st['median']:+7.1f}% ({st['pct_up']:3.0f}%)" if st.get("n") else f"{'-':>13}")
            two_x = "-" if g["pct_2x"] is None else f"{g['pct_2x']:.0f}%"
            halved = "-" if g["pct_halved"] is None else f"{g['pct_halved']:.0f}%"
            lines.append(f"{name:<13}{g['n']:>5} " + "".join(f"{c:>13}" for c in cells) + f"{two_x:>11}{halved:>13}")
    if summary["reject_reasons"]:
        lines.append("")
        lines.append("Alasan ditolak terbanyak:")
        for reason, count in summary["reject_reasons"].items():
            lines.append(f"  {count:>5}  {reason}")
    if summary["rugcheck_dangers"]:
        lines.append("")
        lines.append("Risiko 'danger' RugCheck terbanyak (bisa diabaikan lewat safety.rugcheck.ignore_risks):")
        for risk, count in summary["rugcheck_dangers"].items():
            lines.append(f"  {count:>5}  {risk}")
    p = summary["pnl"]
    lines.append("")
    lines.append("Paper trading:")
    lines.append(f"  beli {p['buys']}, posisi ditutup {p['closed']}, untung {p['wins']}"
                 + ("" if p["win_rate"] is None else f" (win rate {p['win_rate']:.0f}%)"))
    lines.append(f"  P&L terealisasi {p['realized_sol']:+.4f} SOL, fee {p['fees_sol']:.4f} SOL")
    if p["closed"]:
        lines.append(
            f"  rata-rata untung {_fmt(p['avg_win_sol'], ' SOL', 4)}, rata-rata rugi {_fmt(p['avg_loss_sol'], ' SOL', 4)}, "
            f"terbaik {_fmt(p['best_sol'], ' SOL', 4)}, terburuk {_fmt(p['worst_sol'], ' SOL', 4)}"
        )
    if p["exit_reasons"]:
        lines.append("  cara keluar: " + ", ".join(f"{k} {v}x" for k, v in p["exit_reasons"].items()))
    if p["methods"]:
        lines.append("  harga simulasi: " + ", ".join(f"{k} {v}x" for k, v in p["methods"].items()))
    d = p.get("diagnosis") or {"n": 0}
    if d["n"]:
        lines.append("")
        lines.append(f"Diagnosa {d['n']} posisi (harga selama dipegang, dibanding harga beli):")
        lines.append(f"  tidak pernah naik {NEVER_UP_PCT}%: {d['never_up']}  (tanda waktu beli buruk)")
        lines.append(f"  sempat +{GAVE_BACK_PCT}% lalu rugi: {d['gave_back']}  (tanda cara jual buruk)")
        if d["median_entry_age_min"] is not None:
            lines.append(f"  dibeli {d['median_entry_age_min']:.0f} mnt setelah migrasi, ditahan {d['median_held_min'] or 0:.0f} mnt (median)")
    if p.get("recent"):
        lines.append("")
        lines.append("Posisi terakhir (puncak = naik tertinggi setelah dibeli):")
        lines.append(f"  {'token':<9} {'umur':>4} {'puncak':>6} {'P&L':>5}  keluar")
        for r in p["recent"]:
            age = "-" if r["entry_age_min"] is None else f"{r['entry_age_min']:.0f}m"
            peak = "-" if r["peak_pct"] is None else f"{r['peak_pct']:+.0f}%"
            pnl_pct = "-" if r["pnl_pct"] is None else f"{r['pnl_pct']:+.0f}%"
            symbol = ("$" + r["symbol"])[:9]
            lines.append(f"  {symbol:<9} {age:>4} {peak:>6} {pnl_pct:>5}  {r['reason']}")
    lines.append("")
    lines.append("Kesimpulan:")
    for line in summary["verdict"] or ["Belum ada data."]:
        lines.append(f"  - {line}")
    return "\n".join(lines)
