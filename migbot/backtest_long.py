"""`migbot backtest --lama`: older meme coins, 1 to 3 days after migration.

Every real migration is followed for up to 7 days (a sample every 10 minutes,
see long_tracking in the config). This replays entry rules that buy tokens
still alive on day 1, 2 or 3, sells with two exit profiles through the bot's
own evaluate_exit, and charges costs on both sides. A token dropped as dead is
sold at nothing. Trades whose data has not run long enough yet are left out
and counted, so the averages only use finished trades.
"""
from __future__ import annotations

import os
import statistics

from migbot.backtest import simulate_trade
from migbot.config import ExitConfig, Settings
from migbot.storage import iter_long_samples

AGE, PRICE, LIQ, MCAP, VOL, BUYS, SELLS = range(7)  # one sample; age in seconds
HOUR, DAY = 3600, 86_400
TRADEABLE_LIQ, TRADEABLE_MCAP = 10_000.0, 30_000.0
ENTRY_SLACK = 3 * HOUR  # a "day 1" entry is the first sample within 3 hours after the 24-hour mark
DEAD_PRICE = 1e-12  # a token dropped as dead: sold at (practically) nothing
MIN_TRADES = 20

PROFILES = [
    ("1 hari", ExitConfig(stop_loss_pct=30.0, breakeven_after_pct=0.0, take_profit=[[50.0, 0.5]], trailing_start_pct=50.0,
                          trailing_pct=30.0, max_hold_minutes=24 * 60, liquidity_drop_pct=60.0, no_data_exit_minutes=180.0)),
    ("3 hari", ExitConfig(stop_loss_pct=50.0, breakeven_after_pct=0.0, take_profit=[[100.0, 0.5]], trailing_start_pct=80.0,
                          trailing_pct=40.0, max_hold_minutes=72 * 60, liquidity_drop_pct=70.0, no_data_exit_minutes=180.0)),
]


def _f(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def load_series(path: str) -> tuple[dict[str, list[tuple]], float]:
    """mint -> samples sorted by age, and the time of the newest sample."""
    series: dict[str, list[tuple]] = {}
    newest = 0.0
    for row in iter_long_samples(path):
        ts, mint, age_min = _f(row[0]), row[1], _f(row[2])
        price = _f(row[3])
        values = [_f(v) for v in row[4:9]]
        if price <= 0:  # dropped as dead
            sample = (age_min * 60, DEAD_PRICE, 0.0, 0.0, 0.0, 0.0, 0.0)
        else:
            sample = (age_min * 60, price, *values)
        series.setdefault(mint, []).append(sample)
        newest = max(newest, ts)
    for samples in series.values():
        samples.sort()
    return series, newest


def tradeable(p) -> bool:
    return p[PRICE] > DEAD_PRICE and p[LIQ] >= TRADEABLE_LIQ and p[MCAP] >= TRADEABLE_MCAP


def _first_in(points, start: float, end: float) -> int | None:
    for i, p in enumerate(points):
        if p[AGE] > end:
            return None
        if p[AGE] >= start:
            return i
    return None


def _alive_on_day(days: float):
    def rule(points):
        i = _first_in(points, days * DAY, days * DAY + ENTRY_SLACK)
        return i if i is not None and tradeable(points[i]) else None
    return rule


def rule_rising_day1(points):
    """Alive on day 1 and higher than at 12 hours: still going up."""
    i = _alive_on_day(1)(points)
    j = _first_in(points, 12 * HOUR, 12 * HOUR + ENTRY_SLACK)
    return i if i is not None and j is not None and points[i][PRICE] >= points[j][PRICE] else None


def rule_busy_day1(points):
    """Alive on day 1 with $10k+ traded in the last hour."""
    i = _alive_on_day(1)(points)
    return i if i is not None and points[i][VOL] >= 10_000 else None


def rule_dip_day1_3(points):
    """Between day 1 and day 3: 30%+ below its high since 12 hours, and 10%+ off the low since."""
    peak = low = None
    for i, p in enumerate(points):
        if p[AGE] < 12 * HOUR or p[PRICE] <= DEAD_PRICE:
            continue
        if p[AGE] > 3 * DAY:
            return None
        if peak is None or p[PRICE] > peak:
            peak = low = p[PRICE]
        else:
            low = min(low, p[PRICE])
        if p[AGE] >= DAY and p[PRICE] <= 0.7 * peak and p[PRICE] >= 1.1 * low and tradeable(p):
            return i
    return None


RULES = [
    ("hidup hari 1", _alive_on_day(1)),
    ("hidup hari 2", _alive_on_day(2)),
    ("hidup hari 3", _alive_on_day(3)),
    ("naik di hari 1", rule_rising_day1),
    ("ramai di hari 1", rule_busy_day1),
    ("dip hari 1-3", rule_dip_day1_3),
]


def _survival(series: dict) -> list[dict]:
    """Of the tokens followed from their first hours: how many were still tradeable at each age."""
    out = []
    for label, age in (("6 jam", 6 * HOUR), ("1 hari", DAY), ("2 hari", 2 * DAY), ("3 hari", 3 * DAY)):
        eligible = alive = 0
        for samples in series.values():
            if samples[0][AGE] > 3 * HOUR:
                continue  # picked up later (backfill): its early hours are unknown
            last_ts_age = samples[-1][AGE]
            dead = samples[-1][PRICE] <= DEAD_PRICE
            if not dead and last_ts_age < age:
                continue  # not old enough yet to tell
            eligible += 1
            i = _first_in(samples, age, age + ENTRY_SLACK)
            alive += i is not None and tradeable(samples[i])
        out.append({"label": label, "n": eligible, "pct": alive / eligible * 100 if eligible else None})
    return out


def backtest_long(data_dir: str, s: Settings) -> dict:
    series, newest = load_series(os.path.join(data_dir, "long_samples.csv.gz"))
    cost = (s.costs.est_swap_fee_pct + s.costs.extra_slippage_pct) / 100 + s.costs.priority_fee_sol / s.trading.buy_sol
    results = {name: {profile: [] for profile, _ in PROFILES} for name, _ in RULES}
    unfinished = 0
    for points in series.values():
        for name, rule in RULES:
            i = rule(points)
            if i is None or i >= len(points) - 1:
                continue
            for profile, exits in PROFILES:
                pnl, closed = simulate_trade(points, i, exits, cost)
                if closed:
                    results[name][profile].append(pnl)
                else:
                    unfinished += 1
    rows = []
    best = None
    for name, _ in RULES:
        row = {"rule": name}
        for profile, _ in PROFILES:
            pnls = results[name][profile]
            st = {"n": len(pnls)}
            if pnls:
                st.update(avg_pct=statistics.fmean(pnls) * 100, win_pct=sum(p > 0 for p in pnls) / len(pnls) * 100)
                if st["n"] >= MIN_TRADES and (best is None or st["avg_pct"] > best["avg_pct"]):
                    best = {"rule": name, "profile": profile, **st}
            row[profile] = st
        rows.append(row)
    max_age = max((samples[-1][AGE] for samples in series.values()), default=0) / DAY
    return {
        "tokens": len(series), "max_age_days": max_age, "survival": _survival(series), "rows": rows,
        "unfinished": unfinished, "best": best, "cost_pct": cost * 100, "buy_sol": s.trading.buy_sol,
    }


def format_backtest_long(result: dict) -> str:
    lines = ["=" * 46, " Backtest meme coin lama (1-3 hari)", "=" * 46]
    if not result["tokens"]:
        lines.append("Belum ada data token lama (long_samples.csv.gz).")
        lines.append("Bot mengumpulkannya tiap 10 menit; coba lagi")
        lines.append("setelah 2-3 hari.")
        return "\n".join(lines)
    lines.append(f"Data: {result['tokens']} token, sampai umur {result['max_age_days']:.1f} hari.")
    lines.append("Masih bisa diperdagangkan (likuiditas ≥ $10k,")
    lines.append("mcap ≥ $30k) setelah:")
    for sv in result["survival"]:
        value = "belum ada data" if sv["pct"] is None else f"{sv['pct']:.0f}% (n={sv['n']})"
        lines.append(f"  {sv['label']:<7} {value}")
    lines.append("")
    lines.append(f"Biaya {result['cost_pct']:.1f}% tiap beli dan tiap jual.")
    lines.append("1 hari = SL -30%, +50% jual separuh, trailing")
    lines.append("         30%, maks 24 jam")
    lines.append("3 hari = SL -50%, +100% jual separuh, trailing")
    lines.append("         40%, maks 72 jam")
    lines.append("")
    for row in result["rows"]:
        for k, (profile, _) in enumerate(PROFILES):
            st = row[profile]
            cell = "belum ada beli" if not st["n"] else f"{st['avg_pct']:+.0f}% ({st['win_pct']:.0f}% untung, n={st['n']})"
            name = row["rule"] if k == 0 else ""
            lines.append(f"  {name:<16}{profile}: {cell}")
    if result["unfinished"]:
        lines.append(f"Belum selesai (tidak dihitung): {result['unfinished']} beli.")
    lines.append("")
    best = result["best"]
    if best is None:
        lines.append(f"Belum ada aturan dengan {MIN_TRADES}+ beli yang selesai.")
        lines.append("Tunggu data beberapa hari lagi.")
    elif best["avg_pct"] <= 0:
        lines.append("Tidak ada aturan yang untung setelah biaya.")
        lines.append(f"Paling ringan: {best['rule']} ({best['profile']}) {best['avg_pct']:+.0f}% per beli.")
    else:
        total = best["avg_pct"] / 100 * best["n"] * result["buy_sol"]
        lines.append(f"Terbaik: {best['rule']} ({best['profile']})")
        lines.append(f"  {best['avg_pct']:+.1f}% per beli, n={best['n']}, ≈ {total:+.2f} SOL")
        lines.append("  dengan 0.1 SOL per beli. Uji paper dulu sebelum")
        lines.append("  percaya: hasil masa lalu sering tidak terulang.")
        if best["n"] < 100:
            lines.append("  n di bawah 100: masih bisa kebetulan.")
    return "\n".join(lines)
