"""Backtest reporting: console summary, CSV/JSON files and a standalone HTML page."""
from __future__ import annotations

import csv
import html
import json
import math
import os
import time
from dataclasses import asdict

from scalper.backtest import BacktestResult, ClosedTrade, compute_stats, split_stats


def _fmt_time(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))


def _num(v: float, digits: int = 2, sign: bool = False) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and math.isinf(v):
        return "∞"
    s = f"{v:+,.{digits}f}" if sign else f"{v:,.{digits}f}"
    return s


def verdict(stats: dict, oos: dict | None) -> str:
    """Plain-language reading of the numbers (deliberately conservative)."""
    n = stats["trades"]
    if n < 30:
        return (
            f"Only {n} trades: far too few to tell skill from luck. Use more days "
            "(--days 180+) or more symbols before drawing any conclusion."
        )
    if stats["net_profit"] <= 0 or stats["profit_factor"] < 1:
        return "Lost money after fees in this period. Do NOT run these settings live."
    if oos is not None and oos["trades"] >= 10 and (oos["net_profit"] <= 0 or oos["profit_factor"] < 1):
        return (
            "Profitable overall but LOST money in the out-of-sample (most recent) part — "
            "a classic sign of an edge that has faded or was fitted to the past. Not ready."
        )
    if stats["profit_factor"] < 1.2 or (oos is not None and oos["trades"] >= 10 and oos["profit_factor"] < 1.2):
        return (
            "Marginal: a profit factor under 1.2 (overall or out-of-sample) means a small "
            "change in fees, slippage or market regime can flip it negative. Paper trade "
            "it before risking money."
        )
    return (
        "Promising on this data. Next step: paper/testnet trade it for a few weeks and "
        "compare with this backtest. Past results do not guarantee future results."
    )


def format_summary(label: str, results: list[BacktestResult], oos_fraction: float) -> str:
    lines: list[str] = []
    bar = "═" * 78
    all_trades = sorted((t for r in results for t in r.trades), key=lambda t: t.entry_time)

    for r in results:
        if not r.equity_curve:
            lines.append(f"{r.symbol}: no data")
            continue
        parts = split_stats(r, oos_fraction)
        a = parts["all"]
        cols = [("ALL", a)]
        if "in_sample" in parts:
            cols.append(("IN-SAMPLE", parts["in_sample"]))
        if "out_of_sample" in parts:
            cols.append(("OUT-OF-SAMPLE", parts["out_of_sample"]))

        lines.append(bar)
        lines.append(
            f" {label}  {r.symbol}   {_fmt_time(r.start_time)} → {_fmt_time(r.end_time)} UTC "
            f"({a['days']:.1f} days)"
        )
        lines.append(bar)
        header = f" {'':22}" + "".join(f"{name:>17}" for name, _ in cols)
        lines.append(header)

        def row(title: str, key: str, fn) -> None:
            lines.append(f" {title:22}" + "".join(f"{fn(s[key]):>17}" for _, s in cols))

        row("Trades", "trades", lambda v: str(v))
        row("Win rate", "win_rate_pct", lambda v: f"{v:.1f}%")
        row("Profit factor", "profit_factor", lambda v: _num(v))
        row("Net profit", "net_profit", lambda v: _num(v, sign=True))
        row("Return", "return_pct", lambda v: f"{v:+.2f}%")
        row("Max drawdown", "max_drawdown_pct", lambda v: f"{v:.2f}%")
        row("Expectancy / trade", "expectancy", lambda v: _num(v, 3, sign=True))
        row("Average R / trade", "avg_r", lambda v: f"{v:+.3f}R")
        row("Average win", "avg_win", lambda v: _num(v))
        row("Average loss", "avg_loss", lambda v: _num(v))
        row("Fees paid", "fees", lambda v: _num(v))
        row("Funding paid", "funding", lambda v: _num(v))
        row("Sharpe (daily, ann.)", "sharpe", lambda v: f"{v:.2f}")
        row("Worst losing streak", "max_losing_streak", lambda v: str(v))
        row("Trades per day", "trades_per_day", lambda v: f"{v:.2f}")
        row("Avg bars held", "avg_bars_held", lambda v: f"{v:.1f}")
        exits = " · ".join(f"{k} {v}" for k, v in sorted(a["exit_reasons"].items(), key=lambda kv: -kv[1]))
        lines.append(f" Exits: {exits or '-'}   (long {a['longs']} / short {a['shorts']})")
        lines.append(f" Signals: {r.signals}")
        if r.skipped:
            skipped = ", ".join(f"{k} ×{v}" for k, v in r.skipped.most_common(5))
            lines.append(f" Rejected signals: {skipped}")
        lines.append(" Verdict: " + verdict(a, parts.get("out_of_sample")))

    if len(results) > 1:
        total_start = sum(r.start_equity for r in results)
        total_net = sum(r.end_equity - r.start_equity for r in results)
        n = len(all_trades)
        wins = sum(1 for t in all_trades if t.net_pnl > 0)
        gw = sum(t.net_pnl for t in all_trades if t.net_pnl > 0)
        gl = -sum(t.net_pnl for t in all_trades if t.net_pnl <= 0)
        lines.append(bar)
        lines.append(
            f" COMBINED ({len(results)} symbols, each with its own starting balance): "
            f"{n} trades · win {wins / n * 100 if n else 0:.1f}% · PF {_num(gw / gl if gl else math.inf)} · "
            f"net {_num(total_net, sign=True)} on {_num(total_start)}"
        )
    lines.append(bar)
    lines.append(
        " Assumes: fills at next bar open + slippage, stop wins when a bar hits both stop and"
    )
    lines.append(
        " target, taker fees on market/stop fills, maker fees on limit take-profits, funding"
    )
    lines.append(" charged as a cost every 8h. Backtests are optimistic about the future. Always.")
    return "\n".join(lines)


def write_outputs(out_dir: str, label: str, results: list[BacktestResult], oos_fraction: float) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    paths = {
        "trades": os.path.join(out_dir, "trades.csv"),
        "equity": os.path.join(out_dir, "equity.csv"),
        "summary": os.path.join(out_dir, "summary.json"),
        "html": os.path.join(out_dir, "report.html"),
    }
    trades = sorted((t for r in results for t in r.trades), key=lambda t: t.entry_time)
    fields = list(ClosedTrade.__dataclass_fields__)
    with open(paths["trades"], "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["entry_time_utc", "exit_time_utc"] + fields)
        w.writeheader()
        for t in trades:
            d = asdict(t)
            w.writerow({"entry_time_utc": _fmt_time(t.entry_time), "exit_time_utc": _fmt_time(t.exit_time), **d})

    with open(paths["equity"], "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "time_utc", "time_ms", "equity"])
        for r in results:
            for t, eq in _downsample(r.equity_curve, 2000):
                w.writerow([r.symbol, _fmt_time(t), t, f"{eq:.4f}"])

    summary = {
        "label": label,
        "generated_at": _fmt_time(int(time.time() * 1000)),
        "symbols": {r.symbol: split_stats(r, oos_fraction) for r in results},
        "skipped_signals": {r.symbol: dict(r.skipped) for r in results},
    }
    with open(paths["summary"], "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=lambda o: None if isinstance(o, float) and math.isinf(o) else str(o))

    with open(paths["html"], "w", encoding="utf-8") as f:
        f.write(render_html(label, results, oos_fraction))
    return paths


def _downsample(curve: list[tuple[int, float]], max_points: int) -> list[tuple[int, float]]:
    if len(curve) <= max_points:
        return curve
    step = len(curve) / max_points
    out = [curve[int(i * step)] for i in range(max_points)]
    if out[-1] != curve[-1]:
        out.append(curve[-1])
    return out


def _svg_curve(curve: list[tuple[int, float]], w: int = 760, h: int = 220) -> str:
    pts = _downsample(curve, 600)
    if len(pts) < 2:
        return '<p class="dim">Not enough data</p>'
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    t0, t1 = pts[0][0], pts[-1][0]

    def x(t: int) -> float:
        return 4 + (t - t0) / max(1, t1 - t0) * (w - 8)

    def y(v: float) -> float:
        return 8 + (hi - v) / span * (h - 16)

    line = " ".join(f"{'M' if i == 0 else 'L'}{x(t):.1f},{y(v):.1f}" for i, (t, v) in enumerate(pts))
    color = "var(--good)" if vals[-1] >= vals[0] else "var(--bad)"
    base_y = y(vals[0])
    # Text lives outside the SVG: the chart stretches to the page width and
    # would otherwise squash any labels drawn inside it.
    return (
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" aria-label="Equity curve">'
        f'<line x1="0" x2="{w}" y1="{base_y:.1f}" y2="{base_y:.1f}" class="grid" stroke-dasharray="4 4" '
        f'vector-effect="non-scaling-stroke"/>'
        f'<path d="{line}" fill="none" stroke="{color}" stroke-width="2" vector-effect="non-scaling-stroke"/>'
        "</svg>"
        f'<div class="legend">{_fmt_time(t0)[:10]} → {_fmt_time(t1)[:10]} · '
        f"low {lo:,.2f} · high {hi:,.2f} · dashed line = starting balance</div>"
    )


def render_html(label: str, results: list[BacktestResult], oos_fraction: float) -> str:
    cards = []
    for r in results:
        if not r.equity_curve:
            continue
        parts = split_stats(r, oos_fraction)
        a = parts["all"]
        oos = parts.get("out_of_sample")
        kpis = [
            ("Net profit", _num(a["net_profit"], sign=True), a["net_profit"] >= 0),
            ("Return", f"{a['return_pct']:+.2f}%", a["return_pct"] >= 0),
            ("Profit factor", _num(a["profit_factor"]), a["profit_factor"] >= 1),
            ("Win rate", f"{a['win_rate_pct']:.1f}%", None),
            ("Trades", str(a["trades"]), None),
            ("Max drawdown", f"{a['max_drawdown_pct']:.2f}%", None),
            ("Fees paid", _num(a["fees"]), None),
            ("OOS return", f"{oos['return_pct']:+.2f}%" if oos else "-", (oos or {}).get("return_pct", 0) >= 0),
        ]
        kpi_html = "".join(
            f'<div class="kpi"><div class="k">{html.escape(k)}</div>'
            f'<div class="v {"good" if ok else "bad" if ok is False else ""}">{html.escape(v)}</div></div>'
            for k, v, ok in kpis
        )
        rows = "".join(
            f"<tr><td>{_fmt_time(t.entry_time)}</td><td>{t.side}</td><td class=n>{t.entry_price:,.4f}</td>"
            f"<td class=n>{t.exit_price:,.4f}</td><td>{t.exit_reason}</td>"
            f'<td class="n {"good" if t.net_pnl > 0 else "bad"}">{t.net_pnl:+,.2f}</td>'
            f"<td class=n>{t.r_multiple:+.2f}R</td></tr>"
            for t in r.trades[-40:][::-1]
        )
        cards.append(
            f'<section class="card"><h2>{html.escape(r.symbol)}</h2>'
            f'<div class="kpis">{kpi_html}</div>'
            f'<div class="chart">{_svg_curve(r.equity_curve)}</div>'
            f'<p class="verdict">{html.escape(verdict(a, oos))}</p>'
            f'<details><summary>Last {min(40, len(r.trades))} trades</summary><div class="scroll"><table>'
            f"<thead><tr><th>Entry (UTC)</th><th>Side</th><th class=n>Entry</th><th class=n>Exit</th>"
            f"<th>Exit</th><th class=n>Net P&amp;L</th><th class=n>R</th></tr></thead><tbody>{rows}</tbody>"
            f"</table></div></details></section>"
        )
    body = "".join(cards) or '<p class="dim">No results.</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scalper Backtest</title>
<style>
:root {{ --bg:#f6f7f9; --panel:#fff; --border:#e3e6eb; --text:#1b1f27; --dim:#667085; --good:#0f8a5f; --bad:#c2343b; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0b0e14; --panel:#131722; --border:#232838; --text:#e7e9ee; --dim:#8b93a7; --good:#22c3a6; --bad:#f2545b; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--text);
  font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:900px; margin:0 auto; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:0 0 12px; }}
.dim {{ color:var(--dim); }}
.card {{ background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:16px; margin-top:16px; }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; }}
.kpi .k {{ font-size:12px; color:var(--dim); }} .kpi .v {{ font-size:18px; font-weight:600; font-variant-numeric:tabular-nums; }}
.good {{ color:var(--good); }} .bad {{ color:var(--bad); }}
.chart {{ margin:14px 0 6px; }} .chart svg {{ width:100%; height:220px; display:block; }}
.grid {{ stroke:var(--dim); stroke-opacity:.5; }}
.legend {{ font-size:12px; color:var(--dim); margin-top:4px; }}
.verdict {{ background:var(--bg); border-radius:8px; padding:10px 12px; margin:10px 0; }}
.scroll {{ overflow-x:auto; }} table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
th,td {{ padding:6px; border-bottom:1px solid var(--border); text-align:left; white-space:nowrap; }}
.n {{ text-align:right; font-variant-numeric:tabular-nums; }}
summary {{ cursor:pointer; color:var(--dim); }}
</style></head><body><main>
<h1>Backtest: {html.escape(label)}</h1>
<p class="dim">Next-bar fills with slippage, pessimistic intrabar ordering, taker/maker fees and funding included.
Out-of-sample = last {oos_fraction:.0%} of the period. Past results do not guarantee future results.</p>
{body}
</main></body></html>"""


def overall_stats(results: list[BacktestResult]) -> dict:
    trades = sorted((t for r in results for t in r.trades), key=lambda t: t.entry_time)
    curve: list[tuple[int, float]] = []
    start = sum(r.start_equity for r in results)
    eq = start
    for t in trades:
        eq += t.net_pnl
        curve.append((t.exit_time, eq))
    if not curve:
        return compute_stats([], [(0, start)], start)
    return compute_stats(trades, [(trades[0].entry_time, start)] + curve, start)
