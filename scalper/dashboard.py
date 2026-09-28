"""Local, read-only web dashboard for the scalper.

    python -m scalper dashboard            # mode from .env / --mode

Reads the trade journal (data/scalper/trades_<mode>.csv) and state file —
nothing is sent anywhere, no extra dependencies. Refreshes every 5 seconds.
"""
from __future__ import annotations

import json
import math
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scalper.config import Settings
from scalper.journal import StateStore, TradeJournal


def build_summary(rows: list[dict], state: dict, mode: str) -> dict:
    nets = []
    curve = []
    cum = 0.0
    for r in rows:
        try:
            n = float(r.get("net_pnl") or 0)
        except ValueError:
            continue
        nets.append(n)
        cum += n
        curve.append({"t": r.get("exit_time_utc") or r.get("closed_at_utc"), "pnl": round(cum, 6)})
    wins = [n for n in nets if n > 0]
    losses = [-n for n in nets if n <= 0]
    peak = dd = 0.0
    for p in curve:
        peak = max(peak, p["pnl"])
        dd = max(dd, peak - p["pnl"])
    pf = sum(wins) / sum(losses) if sum(losses) > 0 else (None if not wins else math.inf)
    risk = state.get("risk") or {}
    by_symbol: dict[str, dict] = {}
    for r in rows:
        s = by_symbol.setdefault(r.get("symbol", "?"), {"trades": 0, "net": 0.0})
        s["trades"] += 1
        s["net"] += float(r.get("net_pnl") or 0)
    open_trades = []
    for sym, t in (state.get("trades") or {}).items():
        open_trades.append({
            "symbol": sym, "side": t.get("side"), "qty": t.get("qty"), "entry": t.get("entry_price"),
            "stop": t.get("stop"), "stop_kind": t.get("stop_kind"), "target": t.get("take_profit"),
            "bars": t.get("bars_held"),
        })
    balance = None
    if "paper" in state:
        balance = state["paper"].get("balance")
    elif rows:
        try:
            balance = float(rows[-1].get("equity_after") or "nan")
            balance = None if math.isnan(balance) else balance
        except ValueError:
            balance = None
    return {
        "mode": mode,
        "saved_at": state.get("saved_at"),
        "balance": balance,
        "trades": len(nets),
        "net": sum(nets),
        "win_rate": len(wins) / len(nets) * 100 if nets else 0.0,
        "profit_factor": None if pf is None else ("inf" if math.isinf(pf) else round(pf, 3)),
        "max_dd": dd,
        "fees": sum(float(r.get("fees") or 0) for r in rows),
        "today": risk.get("realized_today", 0.0),
        "trades_today": risk.get("trades_today", 0),
        "halted": risk.get("halted", False),
        "halt_reason": risk.get("halt_reason", ""),
        "curve": curve[-1000:],
        "by_symbol": by_symbol,
        "open_trades": open_trades,
        "recent": list(reversed(rows))[:50],
    }


PAGE = """<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scalper Dashboard</title>
<style>
:root { --bg:#f6f7f9; --panel:#fff; --border:#e3e6eb; --text:#1b1f27; --dim:#667085; --good:#0f8a5f; --bad:#c2343b; --accent:#3b6fd8; }
@media (prefers-color-scheme: dark) { :root { --bg:#0b0e14; --panel:#131722; --border:#232838; --text:#e7e9ee; --dim:#8b93a7; --good:#22c3a6; --bad:#f2545b; --accent:#5b8def; } }
* { box-sizing:border-box; }
body { margin:0; padding:20px 16px 48px; background:var(--bg); color:var(--text); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
main { max-width:1100px; margin:0 auto; }
header { display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }
h1 { font-size:20px; margin:0; } h2 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--dim); margin:0 0 10px; }
.dim { color:var(--dim); } .good { color:var(--good); } .bad { color:var(--bad); }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:600; background:var(--border); }
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:16px 0; }
.card { background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:14px 16px; }
.kpi .k { font-size:12px; color:var(--dim); } .kpi .v { font-size:22px; font-weight:600; font-variant-numeric:tabular-nums; }
.grid { display:grid; grid-template-columns:2fr 1fr; gap:12px; margin-bottom:12px; }
@media (max-width:800px) { .grid { grid-template-columns:1fr; } }
svg { width:100%; height:220px; display:block; }
.scroll { overflow:auto; max-height:420px; }
table { width:100%; border-collapse:collapse; font-size:12.5px; }
th,td { padding:6px; border-bottom:1px solid var(--border); text-align:left; white-space:nowrap; }
th { color:var(--dim); font-weight:500; } .n { text-align:right; font-variant-numeric:tabular-nums; }
.alert { background:var(--bad); color:#fff; border-radius:10px; padding:10px 14px; margin-top:12px; }
.demo { border:1px dashed var(--accent); color:var(--text); border-radius:10px; padding:10px 14px; margin-top:12px; font-size:13px; }
.demo code { background:var(--border); padding:1px 5px; border-radius:4px; white-space:nowrap; }
.legend { display:flex; justify-content:space-between; font-size:12px; color:var(--dim); margin-top:6px; font-variant-numeric:tabular-nums; }
@media (max-width:600px) { .hide-sm { display:none; } }
</style></head><body><main>
<header><h1>Scalper Dashboard <span class="badge" id="mode">-</span></h1><span class="dim" id="status">memuat...</span></header>
<div id="alert"></div>
<div class="demo" id="demo" hidden><strong>DEMO</strong> &mdash; trade di bawah ini dibuat oleh logika bot yang asli, tetapi
di atas <strong>harga acak (pasar palsu)</strong>. Di pasar acak tidak ada strategi yang punya edge, jadi angka P&amp;L di sini
hanya noise dikurangi fee: <strong>bukan hasil trading dan bukan perkiraan profit</strong>. Untuk data sungguhan, jalankan
<code>python -m scalper run</code> (mode paper) lalu buka dashboard tanpa <code>--demo</code>.</div>
<div class="kpis">
 <div class="card kpi"><div class="k">Saldo</div><div class="v" id="k-bal">-</div></div>
 <div class="card kpi"><div class="k">Net P&amp;L</div><div class="v" id="k-net">-</div></div>
 <div class="card kpi"><div class="k" id="k-today-label">Hari ini</div><div class="v" id="k-today">-</div></div>
 <div class="card kpi"><div class="k">Win rate</div><div class="v" id="k-win">-</div></div>
 <div class="card kpi"><div class="k">Profit factor</div><div class="v" id="k-pf">-</div></div>
 <div class="card kpi"><div class="k">Max drawdown (nilai)</div><div class="v" id="k-dd">-</div></div>
 <div class="card kpi"><div class="k">Total fee</div><div class="v" id="k-fee">-</div></div>
 <div class="card kpi"><div class="k">Jumlah trade</div><div class="v" id="k-n">-</div></div>
</div>
<div class="grid">
 <div class="card"><h2>Kurva P&amp;L kumulatif</h2><svg id="curve" viewBox="0 0 700 220" preserveAspectRatio="none" role="img" aria-label="Kurva P&amp;L kumulatif"></svg><div class="legend"><span id="lg-lo"></span><span id="lg-hi"></span></div></div>
 <div class="card"><h2>Posisi terbuka</h2><div class="scroll"><table><thead><tr><th>Simbol</th><th>Arah</th><th class=n>Entry</th><th class=n>Stop</th><th class=n>Target</th></tr></thead><tbody id="open"></tbody></table></div></div>
</div>
<div class="card"><h2>Trade terakhir</h2><div class="scroll"><table><thead><tr><th>Tutup UTC</th><th>Simbol</th><th class="hide-sm">Arah</th><th class="n hide-sm">Entry</th><th class="n hide-sm">Exit</th><th>Alasan</th><th class=n>Net</th><th class=n>R</th></tr></thead><tbody id="recent"></tbody></table></div></div>
<p class="dim">Hanya-baca, 100% lokal. Refresh tiap 5 detik.</p>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = (v, d=2) => (v === null || v === undefined || v === '') ? '-' : Number(v).toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d});
const signed = (v) => (v > 0 ? '+' : '') + num(v);
function setVal(id, text, cls) { const el = $(id); el.textContent = text; el.className = 'v ' + (cls || ''); }
function curve(points) {
  const svg = $('curve'), w = 700, h = 220, pl = 4, pr = 4, pt = 10, pb = 10;
  if (points.length < 2) { svg.innerHTML = ''; $('lg-lo').textContent = 'Belum ada trade'; $('lg-hi').textContent = ''; return; }
  const vals = [0, ...points.map(p => p.pnl)], lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
  const x = i => pl + i / (vals.length - 1) * (w - pl - pr), y = v => pt + (hi - v) / span * (h - pt - pb);
  const d = vals.map((v, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1)).join(' ');
  const color = vals[vals.length - 1] >= 0 ? 'var(--good)' : 'var(--bad)';
  svg.innerHTML = `<line x1="${pl}" x2="${w-pr}" y1="${y(0)}" y2="${y(0)}" stroke="var(--dim)" stroke-opacity=".5" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
  $('lg-lo').textContent = 'Terendah ' + signed(lo);
  $('lg-hi').textContent = 'Tertinggi ' + signed(hi) + ' · garis putus = 0';
}
async function refresh() {
  try {
    const d = await (await fetch('/api/data', {cache: 'no-store'})).json();
    $('mode').textContent = d.mode.toUpperCase();
    setVal('k-bal', d.balance === null ? '-' : num(d.balance));
    setVal('k-net', signed(d.net), d.net > 0 ? 'good' : d.net < 0 ? 'bad' : '');
    setVal('k-today', signed(d.today), d.today > 0 ? 'good' : d.today < 0 ? 'bad' : '');
    $('k-today-label').textContent = `Hari ini (${d.trades_today} trade)`;
    setVal('k-win', num(d.win_rate, 1) + '%');
    setVal('k-pf', d.profit_factor === null ? '-' : String(d.profit_factor));
    setVal('k-dd', d.max_dd > 0 ? '-' + num(d.max_dd) : num(0), d.max_dd > 0 ? 'bad' : '');
    setVal('k-fee', num(d.fees));
    setVal('k-n', String(d.trades));
    $('alert').innerHTML = d.halted ? `<div class="alert">BOT BERHENTI: ${esc(d.halt_reason)}</div>` : '';
    $('demo').hidden = d.mode !== 'demo';
    curve(d.curve);
    $('open').innerHTML = d.open_trades.map(t => `<tr><td>${esc(t.symbol)}</td><td>${esc(t.side)}</td><td class=n>${num(t.entry,4)}</td><td class=n>${num(t.stop,4)} <span class="dim">${esc(t.stop_kind)}</span></td><td class=n>${num(t.target,4)}</td></tr>`).join('') || '<tr><td colspan=5 class="dim">Tidak ada</td></tr>';
    $('recent').innerHTML = d.recent.map(r => `<tr><td>${esc(String(r.closed_at_utc || '').slice(5, 16))}</td><td>${esc(r.symbol)}</td><td class="hide-sm">${esc(r.side)}</td><td class="n hide-sm">${num(r.entry_price,4)}</td><td class="n hide-sm">${num(r.exit_price,4)}</td><td>${esc(r.exit_reason)}</td><td class="n ${Number(r.net_pnl) > 0 ? 'good' : 'bad'}">${signed(Number(r.net_pnl))}</td><td class=n>${num(r.r_multiple)}R</td></tr>`).join('') || '<tr><td colspan=8 class="dim">Belum ada trade</td></tr>';
    $('status').textContent = 'update ' + new Date().toLocaleTimeString('id-ID') + (d.saved_at ? ' · state ' + d.saved_at + ' UTC' : '');
  } catch (e) { $('status').textContent = 'gagal memuat: ' + e; }
}
refresh(); setInterval(refresh, 5000);
</script></body></html>"""


def serve(settings: Settings, port: int = 8766, open_browser: bool = True) -> None:
    d = settings.data_dir
    journal_path = os.path.join(d, f"trades_{settings.mode}.csv")
    state_path = os.path.join(d, f"state_{settings.mode}.json")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - keep the console quiet
            pass

        def _send(self, body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.startswith("/api/data"):
                rows = TradeJournal(journal_path).read() if os.path.exists(journal_path) else []
                state = StateStore(state_path).peek()
                self._send(json.dumps(build_summary(rows, state, settings.mode)).encode(), "application/json")
            else:
                self._send(PAGE.encode(), "text/html; charset=utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Dashboard: {url}  (Ctrl+C to stop)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
