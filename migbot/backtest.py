"""`migbot backtest`: which entry rule would have made money?

Replays entry rules on the price path the bot recorded for every real
migration (a point every 10 s for 2 hours), sells with the bot's exit rules,
and charges costs on every buy and sell. Every rule is tried on every token,
so rules are compared on the same tokens, in a minute instead of weeks of
paper trading.

Limits: prices are DexScreener's, 10 s apart, so a crash between two points
is sold at the next point, as in live trading; holder data is the snapshot
from the start of the buy window; one entry per token per rule.
"""
from __future__ import annotations

import os
import statistics

from migbot.config import ExitConfig, Settings
from migbot.storage import iter_paths, read_csv
from migbot.trading import Position, evaluate_exit

T, PRICE, LIQ, MCAP, VOL, BUYS, SELLS = range(7)  # one recorded point
JUNK_SOURCE = "geckoterminal"  # seen only by GeckoTerminal: a new pool for an old token, not a migration
MIN_TRADES = 30  # rules with fewer trades are listed but never called the best
REAL_POOL_USD = 5_000  # a fresh migration starts with far more liquidity than this
SIZE = 10_000  # position units for the exit rules (only fractions matter)

LOOSE_EXITS = ExitConfig(
    stop_loss_pct=50.0, breakeven_after_pct=0.0, take_profit=[[100.0, 0.5]], trailing_start_pct=60.0,
    trailing_pct=30.0, max_hold_minutes=60.0, liquidity_drop_pct=70.0, no_data_exit_minutes=10.0,
)


def _num(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).rstrip("+"))
    except ValueError:
        return None


def _txns(p) -> int | None:
    return None if p[BUYS] is None or p[SELLS] is None else p[BUYS] + p[SELLS]


def _first_at(points: list, seconds: float) -> int | None:
    for i, p in enumerate(points):
        if p[T] >= seconds:
            return i
    return None


# --------------------------------------------------------------------- entry rules
# Each takes (points, the token's research row, settings) and returns the index of
# the point to buy at, or None. They only look at points up to that index.

def rule_any(points, feat, s):
    """Every real migration at the start of the buy window: the baseline."""
    i = _first_at(points, s.entry.delay_seconds)
    return i if i is not None and (points[i][LIQ] or 0) >= REAL_POOL_USD else None


def rule_busy(points, feat, s):
    """Very busy at the start of the window (1000+ transactions in 5 minutes): "trending" at launch."""
    i = rule_any(points, feat, s)
    return i if i is not None and (_txns(points[i]) or 0) >= 1000 else None


def rule_calm(points, feat, s):
    """Quiet but alive at the start of the window (10-300 transactions in 5 minutes)."""
    i = rule_any(points, feat, s)
    txns = _txns(points[i]) if i is not None else None
    return i if txns is not None and 10 <= txns <= 300 else None


def rule_calm_spread(points, feat, s):
    """Quiet, and supply spread out: top 50 wallets ≤ 25%, at most one wallet ≥ 1%."""
    i = rule_calm(points, feat, s)
    top50, big = _num(feat.get("top50_pct")), _num(feat.get("wallets_1pct"))
    return i if i is not None and top50 is not None and big is not None and top50 <= 25 and big <= 1 else None


def rule_survivor(points, feat, s):
    """Still standing 30 minutes after migration: at least half the start-of-window price, pool intact."""
    i5, i30 = _first_at(points, s.entry.delay_seconds), _first_at(points, 1800)
    if i5 is None or i30 is None:
        return None
    p5, p30 = points[i5][PRICE], points[i30][PRICE]
    return i30 if p5 and p30 and p30 >= 0.5 * p5 and (points[i30][LIQ] or 0) >= 10_000 else None


def rule_trending_late(points, feat, s):
    """Busy again 30-60 minutes after migration, near its high: the trending idea after the first dump."""
    start = _first_at(points, s.entry.delay_seconds)
    if start is None:
        return None
    high = 0.0
    for i in range(start, len(points)):
        p = points[i]
        if p[T] > 3600:
            return None
        high = max(high, p[PRICE] or 0.0)
        if p[T] >= 1800 and (_txns(p) or 0) >= 300 and p[PRICE] and p[PRICE] >= 0.9 * high and (p[LIQ] or 0) >= 10_000:
            return i
    return None


def _holders_ok(feat: dict, s: Settings) -> bool:
    """The bot's holder checks on the start-of-window snapshot; missing data passes, as in the bot."""
    f, rc = s.filters, s.safety.rugcheck
    checks = [
        (_num(feat.get("holders")), lambda v: f.min_holders <= 0 or v >= f.min_holders),
        (_num(feat.get("top10_pct")), lambda v: v <= f.max_top10_pct),
        (_num(feat.get("top_holder_pct")), lambda v: v <= f.max_top_holder_pct),
        (_num(feat.get("dev_pct")), lambda v: v <= f.max_dev_hold_pct),
    ]
    if any(value is not None and not ok(value) for value, ok in checks):
        return False
    ignore = {r.lower() for r in rc.ignore_risks}
    danger = [r for r in (feat.get("rugcheck_danger") or "").split("; ") if r and r.lower() not in ignore]
    return not (rc.enabled and rc.reject_danger and danger)


def _market_ok(p, first: float, s: Settings) -> bool:
    f = s.filters
    txns = _txns(p)
    tests = [
        (p[LIQ], lambda v: v >= f.min_liquidity_usd),
        (p[MCAP], lambda v: v >= f.min_market_cap_usd and (f.max_market_cap_usd <= 0 or v <= f.max_market_cap_usd)),
        (p[VOL], lambda v: v >= f.min_volume_5m_usd),
        (txns, lambda v: v >= f.min_txns_5m),
        (p[BUYS] / txns if txns else None, lambda v: v >= f.min_buy_ratio_5m),
    ]
    if any(value is not None and not ok(value) for value, ok in tests):
        return False
    change = (p[PRICE] / first - 1) * 100
    return -f.max_drop_from_first_pct <= change <= f.max_rise_from_first_pct


def rule_bot(points, feat, s):
    """The bot's own rule from config: dip (if on), market filters, holder checks."""
    if not _holders_ok(feat, s):
        return None
    e = s.entry
    first = peak = low = None
    for i, p in enumerate(points):
        price = p[PRICE]
        if not price:
            continue
        first = first or price
        if peak is None or price > peak:
            peak = low = price
        else:
            low = min(low, price)
        if p[T] < e.delay_seconds:
            continue
        if p[T] > e.window_seconds:
            return None
        if e.dip_pct > 0 and ((1 - price / peak) * 100 < e.dip_pct or (price / low - 1) * 100 < e.dip_bounce_pct):
            continue
        if _market_ok(p, first, s):
            return i
    return None


RULES = [
    ("acak menit 5", rule_any),
    ("ramai menit 5", rule_busy),
    ("sepi menit 5", rule_calm),
    ("sepi+tersebar", rule_calm_spread),
    ("aturan bot", rule_bot),
    ("bertahan 30m", rule_survivor),
    ("trending 30-60m", rule_trending_late),
]


# --------------------------------------------------------------------- one trade
def simulate(points: list, i: int, exits: ExitConfig, cost: float) -> float:
    """Profit or loss of one buy at points[i], as a fraction of the money put in.

    The buy pays `cost` on top of the price and every sale loses `cost`; the exits
    are the bot's own evaluate_exit, run on every later point. Whatever is still
    held when the points run out is sold at the last price.
    """
    return simulate_trade(points, i, exits, cost)[0]


def simulate_trade(points: list, i: int, exits: ExitConfig, cost: float) -> tuple[float, bool]:
    """(profit or loss, closed): closed is False when the points ran out before an exit."""
    entry_t, entry_price = points[i][T], points[i][PRICE]
    effective = entry_price * (1 + cost)
    pos = Position(
        mint="", symbol="", pair_address="", opened_at=entry_t, cost_sol=1.0, tokens_raw_initial=SIZE,
        tokens_raw=SIZE, decimals=0, entry_price_native=effective, entry_price_usd=entry_price, entry_mcap_usd=None,
        liquidity_at_entry=points[i][LIQ], peak_price_native=entry_price, last_price_native=entry_price,
        last_price_at=entry_t, method="backtest",
    )
    proceeds = 0.0
    for p in points[i + 1:]:
        price = p[PRICE]
        if not price:
            continue
        pos.last_price_native, pos.last_price_at = price, p[T]
        pos.peak_price_native = max(pos.peak_price_native, price)
        decision = evaluate_exit(pos, price, p[LIQ], p[T], exits)
        if decision is None:
            continue
        sold = min(decision.tokens_raw, pos.tokens_raw)
        proceeds += sold / SIZE * price / effective * (1 - cost)
        pos.tokens_raw -= sold
        if decision.tp_index is not None:
            pos.tp_done.append(decision.tp_index)
        if pos.tokens_raw <= 0:
            return proceeds - 1.0, True
    last = next((p[PRICE] for p in reversed(points) if p[PRICE]), entry_price)
    return proceeds + pos.tokens_raw / SIZE * last / effective * (1 - cost) - 1.0, False


# --------------------------------------------------------------------- the run
def _stats(pnls: list[float]) -> dict:
    if not pnls:
        return {"n": 0}
    return {
        "n": len(pnls),
        "win_pct": sum(p > 0 for p in pnls) / len(pnls) * 100,
        "avg_pct": statistics.fmean(pnls) * 100,
        "median_pct": statistics.median(pnls) * 100,
    }


def backtest(data_dir: str, s: Settings) -> dict:
    features = {row.get("mint"): row for row in read_csv(os.path.join(data_dir, "tokens.csv"))}
    cost = (s.costs.est_swap_fee_pct + s.costs.extra_slippage_pct) / 100 + s.costs.priority_fee_sol / s.trading.buy_sol
    profiles = [("ketat", s.exits), ("longgar", LOOSE_EXITS)]
    pnls = {name: {profile: [] for profile, _ in profiles} for name, _ in RULES}
    tokens = junk = 0
    for record in iter_paths(os.path.join(data_dir, "paths.jsonl.gz")):
        feat = features.get(record.get("mint")) or {}
        if feat.get("source") == JUNK_SOURCE:
            junk += 1
            continue
        points = [p for p in record.get("points") or [] if isinstance(p, list) and len(p) >= 7 and p[PRICE]]
        if len(points) < 2:
            continue
        tokens += 1
        for name, rule in RULES:
            i = rule(points, feat, s)
            if i is None or i >= len(points) - 1:
                continue
            for profile, exits in profiles:
                pnls[name][profile].append(simulate(points, i, exits, cost))
    rows = [{"rule": name, **{profile: _stats(pnls[name][profile]) for profile, _ in profiles}} for name, _ in RULES]
    best = None
    for row in rows:
        for profile, _ in profiles:
            st = row[profile]
            if st["n"] >= MIN_TRADES and (best is None or st["avg_pct"] > best["avg_pct"]):
                best = {"rule": row["rule"], "profile": profile, **st}
    return {"tokens": tokens, "junk": junk, "cost_pct": cost * 100, "rows": rows, "best": best,
            "buy_sol": s.trading.buy_sol, "exits": s.exits}


def _cell(st: dict) -> str:
    return "-" if not st["n"] else f"{st['avg_pct']:+.0f}% ({st['win_pct']:.0f}%)"


def format_backtest(result: dict) -> str:
    lines = ["=" * 46, f" Backtest: aturan beli pada {result['tokens']} migrasi", "=" * 46]
    if not result["tokens"]:
        lines.append("Belum ada catatan harga (data/migbot/paths.jsonl.gz).")
        lines.append("Bot mencatatnya saat token selesai dipantau (2 jam).")
        return "\n".join(lines)
    x = result["exits"]
    lines.append(f"Biaya {result['cost_pct']:.1f}% tiap beli dan tiap jual.")
    lines.append("Angka = rata-rata untung/rugi per beli,")
    lines.append("(dalam kurung: berapa % beli yang untung).")
    lines.append(f"ketat   = aturan bot: SL -{x.stop_loss_pct:g}%, impas +{x.breakeven_after_pct:g}%,")
    lines.append(f"          maks {x.max_hold_minutes:g} mnt")
    lines.append("longgar = SL -50%, TP +100% separuh,")
    lines.append("          trailing 30% dari +60%, maks 60 mnt")
    if result["junk"]:
        lines.append(f"Tidak dihitung: {result['junk']} pool sampah.")
    lines.append("")
    lines.append(f"  {'aturan':<15}{'n':>5}  {'ketat':<11} longgar")
    for row in result["rows"]:
        n = max(row["ketat"]["n"], row["longgar"]["n"])
        lines.append(f"  {row['rule']:<15}{n:>5}  {_cell(row['ketat']):<11} {_cell(row['longgar'])}")
    lines.append("")
    best = result["best"]
    if best is None:
        lines.append(f"Belum ada aturan dengan {MIN_TRADES}+ beli.")
    elif best["avg_pct"] <= 0:
        lines.append("Tidak ada aturan yang untung setelah biaya.")
        lines.append(f"Paling ringan: {best['rule']} ({best['profile']}) {best['avg_pct']:+.0f}% per beli.")
    else:
        total = best["avg_pct"] / 100 * best["n"] * result["buy_sol"]
        lines.append(f"Terbaik: {best['rule']} ({best['profile']})")
        lines.append(f"  {best['avg_pct']:+.1f}% per beli, n={best['n']}, ≈ {total:+.2f} SOL")
        lines.append("  dengan 0.1 SOL per beli. Hasil masa lalu sering")
        lines.append("  tidak terulang: uji paper dulu sebelum percaya.")
        if best["n"] < 100:
            lines.append("  n di bawah 100: masih bisa kebetulan.")
    return "\n".join(lines)
