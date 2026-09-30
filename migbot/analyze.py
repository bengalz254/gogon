"""`migbot analyze`: which migrated tokens crash, and can anything seen at the
start of the buy window tell them apart beforehand?

Every token has its holder data and market data recorded at the same moment,
the reference point (the start of the buy window). For each measure the tokens
are split into three groups (low / middle / high values) and each group shows
how many crashed (fell 80% or more below the reference price while tracked)
and the median change after 60 minutes. A measure can only work as a filter
when one of its groups crashes clearly less often than the others.
"""
from __future__ import annotations

import os
import statistics

from migbot.filters import fmt_usd
from migbot.storage import read_csv

CRASH_PCT = -80.0
MIN_GROUP = 10  # groups smaller than this are shown but never picked as the headline
RELIABLE_N = 150  # below this, a difference of 10-15 points between groups can be chance

# (label, tokens.csv column, how to show a value)
MEASURES = [
    ("jumlah holder", "holders", "int"),
    ("dompet pegang ≥1% supply", "wallets_1pct", "int"),
    ("50 dompet terbesar pegang", "top50_pct", "pct"),
    ("10 dompet terbesar pegang", "top10_pct", "pct"),
    ("1 dompet terbesar pegang", "top_holder_pct", "pct"),
    ("dev pegang", "dev_pct", "pct"),
    ("skor risiko RugCheck", "rugcheck_score", "int"),
    ("mcap", "ref_mcap_usd", "usd"),
    ("likuiditas", "ref_liquidity_usd", "usd"),
    ("volume 5 menit", "ref_volume_5m_usd", "usd"),
    ("transaksi 5 menit", "ref_txns_5m", "int"),
    ("porsi transaksi beli", "ref_buy_ratio_5m", "ratio"),
]


def _num(value) -> float | None:
    """A CSV cell as a number; "2000+" (more than were counted) reads as 2000."""
    if value in (None, ""):
        return None
    try:
        return float(str(value).rstrip("+"))
    except ValueError:
        return None


def _show(value: float, kind: str) -> str:
    if kind == "usd":
        return fmt_usd(value)
    if kind == "pct":
        return f"{value:.0f}%"
    if kind == "ratio":
        return f"{value * 100:.0f}%"
    return f"{value:.0f}"


def _group(tokens: list[dict], label: str) -> dict:
    rets = [t["ret_60m"] for t in tokens if t["ret_60m"] is not None]
    return {
        "label": label,
        "n": len(tokens),
        "crash_pct": sum(t["crashed"] for t in tokens) / len(tokens) * 100,
        "median_60m": statistics.median(rets) if rets else None,
    }


def _thirds(pairs: list[tuple[float, dict]], kind: str) -> list[dict]:
    """Low / middle / high thirds by value. Equal values stay in one group, so groups can differ in size."""
    values = sorted(v for v, _ in pairs)
    n = len(values)
    q1, q2 = values[(n - 1) // 3], values[2 * (n - 1) // 3]
    buckets = [
        [p for p in pairs if p[0] <= q1],
        [p for p in pairs if q1 < p[0] <= q2],
        [p for p in pairs if p[0] > q2],
    ]
    groups = []
    for bucket in buckets:
        if not bucket:
            continue
        lo, hi = min(v for v, _ in bucket), max(v for v, _ in bucket)
        label = _show(lo, kind) if lo == hi else f"{_show(lo, kind)}–{_show(hi, kind)}"
        groups.append(_group([t for _, t in bucket], label))
    return groups


def analyze(data_dir: str) -> dict:
    tokens = []
    for row in read_csv(os.path.join(data_dir, "tokens.csv")):
        low = _num(row.get("min_ret_pct"))
        if low is None:  # never had a reference price: no market data
            continue
        tokens.append({"row": row, "crashed": low <= CRASH_PCT, "ret_60m": _num(row.get("ret_60m"))})
    result = {"n": len(tokens), "overall": _group(tokens, "semua") if tokens else None, "measures": [], "best": None}
    if not tokens:
        return result
    for label, column, kind in MEASURES:
        pairs = [(v, t) for t in tokens if (v := _num(t["row"].get(column))) is not None]
        if len(pairs) < 3:
            continue
        groups = _thirds(pairs, kind)
        result["measures"].append({"label": label, "n": len(pairs), "groups": groups})
        big = [g for g in groups if g["n"] >= MIN_GROUP]
        if len(big) >= 2:
            best = min(big, key=lambda g: g["crash_pct"])
            worst = max(big, key=lambda g: g["crash_pct"])
            spread = worst["crash_pct"] - best["crash_pct"]
            if result["best"] is None or spread > result["best"]["spread"]:
                result["best"] = {"label": label, "spread": spread, "best": best, "worst": worst}
    sources: dict[str, list[dict]] = {}
    for t in tokens:
        sources.setdefault(t["row"].get("source") or "?", []).append(t)
    result["sources"] = [_group(ts, name) for name, ts in sorted(sources.items())]
    return result


def _line(g: dict) -> str:
    median = "-" if g["median_60m"] is None else f"{g['median_60m']:+.0f}%"
    return f"  {g['label']:<14} n={g['n']:<4} hancur {g['crash_pct']:3.0f}%  60m {median:>5}"


def format_analysis(result: dict) -> str:
    lines = ["=" * 46, " Analisa: token mana yang hancur?", "=" * 46]
    if not result["n"]:
        lines.append("Belum ada token yang selesai dipantau (butuh 2 jam setelah migrasi).")
        return "\n".join(lines)
    o = result["overall"]
    median = "-" if o["median_60m"] is None else f"{o['median_60m']:+.0f}%"
    lines.append(f"Hancur = pernah turun {-CRASH_PCT:.0f}%+ dari harga di awal")
    lines.append("jendela beli. Angka kiri = nilai saat itu.")
    lines.append(f"Semua: {o['n']} token, hancur {o['crash_pct']:.0f}%, median 60m {median}")
    b = result["best"]
    if b:
        lines.append("")
        lines.append(f"Paling membedakan: {b['label']}")
        lines.append(f"  {b['best']['label']}: hancur {b['best']['crash_pct']:.0f}% (n={b['best']['n']})")
        lines.append(f"  {b['worst']['label']}: hancur {b['worst']['crash_pct']:.0f}% (n={b['worst']['n']})")
    if result["n"] < RELIABLE_N:
        lines.append(f"Baru {result['n']} token: selisih 10-15 poin masih")
        lines.append(f"bisa kebetulan. Lebih pasti setelah {RELIABLE_N}+ token.")
    for m in result["measures"]:
        lines.append("")
        lines.append(f"{m['label']} (n={m['n']})")
        lines.extend(_line(g) for g in m["groups"])
    if len(result.get("sources", [])) > 1:
        lines.append("")
        lines.append("sumber deteksi")
        lines.extend(_line(g) for g in result["sources"])
    return "\n".join(lines)
