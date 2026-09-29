"""Local read-only dashboard: python -m migbot dashboard (http://127.0.0.1:8780).

Reads data/migbot/status.json (written by the bot every tick) and the CSV
journals. It cannot place, change or cancel anything; stopping it never
affects the bot. Self-contained page: no CDN, works offline.
"""
from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from migbot.report import summarize
from migbot.storage import clean, read_csv

STALE_S = 30


def load_status(data_dir: str) -> dict:
    path = os.path.join(data_dir, "status.json")
    try:
        with open(path, encoding="utf-8") as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        return {"missing": True}
    status["stale"] = time.time() - float(status.get("updated_at") or 0) > STALE_S
    return status


def load_report(data_dir: str) -> dict:
    report = summarize(data_dir)
    report["recent_trades"] = read_csv(os.path.join(data_dir, "trades.csv"))[-40:][::-1]
    return report


class Handler(BaseHTTPRequestHandler):
    data_dir = "data/migbot"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data) -> None:
        body = json.dumps(clean(data), ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(load_status(self.data_dir))
        elif path == "/api/report":
            self._json(load_report(self.data_dir))
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def make_server(data_dir: str, host: str = "127.0.0.1", port: int = 8780) -> ThreadingHTTPServer:
    handler = type("MigbotHandler", (Handler,), {"data_dir": data_dir})
    return ThreadingHTTPServer((host, port), handler)


def serve(data_dir: str, host: str = "127.0.0.1", port: int = 8780, open_browser: bool = True) -> int:
    try:
        server = make_server(data_dir, host, port)
    except OSError as exc:
        print(f"GAGAL: port {port} tidak bisa dipakai ({exc}). Coba --port {port + 1}.")
        return 1
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    print(f"Dashboard: {url}  (Ctrl+C untuk berhenti; bot tidak terpengaruh)")
    if open_browser and not os.environ.get("SSH_CONNECTION"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Migrated Meme Bot</title>
<link rel="icon" href="data:,">
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #6f6d68;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --wash-1: rgba(42,120,214,0.10);
  --up: #006300; --down: #b42f2f;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --chip: #f0efec;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #9a9890;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --series-1: #3987e5; --wash-1: rgba(57,135,229,0.12);
    --up: #0ca30c; --down: #e66767; --chip: #262624;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #9a9890;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --series-1: #3987e5; --wash-1: rgba(57,135,229,0.12);
  --up: #0ca30c; --down: #e66767; --chip: #262624;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color: inherit; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 16px; }
header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin-bottom: 14px; }
h1 { font-size: 18px; margin: 0; font-weight: 650; }
h2 { font-size: 14px; margin: 0 0 10px; font-weight: 650; }
.sub { color: var(--ink-2); }
.muted { color: var(--muted); }
.spacer { flex: 1; }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px;
  background: var(--chip); font-size: 12px; font-weight: 600; white-space: nowrap; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
button.theme { border: 1px solid var(--ring); background: var(--surface); color: var(--ink);
  border-radius: 8px; padding: 4px 10px; font: inherit; cursor: pointer; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 14px 16px; margin-bottom: 14px; }
.hero { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 18px; }
.hero .value { font-size: clamp(34px, 10vw, 48px); font-weight: 650; line-height: 1.05; letter-spacing: -0.5px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 14px; }
.tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 12px 14px; }
.tile .label { color: var(--ink-2); font-size: 12px; }
.tile .value { font-size: clamp(18px, 5vw, 22px); font-weight: 650; margin-top: 2px; }
.tile .note { color: var(--muted); font-size: 12px; margin-top: 2px; }
.up { color: var(--up); } .down { color: var(--down); }
.table-wrap { overflow-x: auto; margin: 0 -4px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; white-space: nowrap; }
th { color: var(--ink-2); font-weight: 600; font-size: 12px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.wrap-cell { white-space: normal; min-width: 220px; }
.status { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; }
.checks { margin: 6px 0 0; padding: 0; list-style: none; font-size: 12px; }
.checks li { display: flex; gap: 6px; }
.checks .ic { width: 14px; flex: none; text-align: center; font-weight: 700; }
details summary { cursor: pointer; color: var(--ink-2); font-size: 12px; }
.feeds { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 8px; }
.feed { display: flex; gap: 8px; align-items: flex-start; padding: 8px 10px; border-radius: 10px; background: var(--page); }
.feed .name { font-weight: 600; }
.feed .detail { color: var(--ink-2); font-size: 12px; word-break: break-word; }
.chart-box { position: relative; }
.chart-box svg { display: block; width: 100%; height: auto; overflow: visible; touch-action: pan-y; }
.chart-box svg:focus { outline: 2px solid var(--series-1); outline-offset: 4px; border-radius: 4px; }
.tip { position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--ring);
  border-radius: 8px; padding: 6px 9px; font-size: 12px; box-shadow: 0 4px 14px rgba(0,0,0,0.12); display: none; white-space: nowrap; }
.tip strong { font-size: 13px; display: block; }
.empty { color: var(--muted); padding: 18px 0; text-align: center; }
.activity { list-style: none; margin: 0; padding: 0; max-height: 340px; overflow-y: auto; }
.activity li { display: flex; gap: 10px; padding: 5px 0; border-bottom: 1px solid var(--grid); font-size: 13px; }
.activity time { color: var(--muted); font-variant-numeric: tabular-nums; flex: none; min-width: 44px; white-space: nowrap; }
.verdict { margin: 8px 0 0; padding-left: 18px; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
.grid2 > .card { margin-bottom: 0; }
.banner { border-left: 4px solid var(--critical); }
footer { color: var(--muted); font-size: 12px; text-align: center; padding: 8px 0 20px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Migrated Meme Bot</h1>
    <span class="pill">PAPER</span>
    <span class="pill" id="run-pill"><span class="dot" id="run-dot"></span><span id="run-text">memuat…</span></span>
    <span class="spacer"></span>
    <span class="muted" id="updated"></span>
    <button class="theme" id="theme-btn" type="button">Tema</button>
  </header>

  <div class="card banner" id="banner" hidden></div>

  <section class="card">
    <div class="hero">
      <div>
        <div class="sub">P&amp;L paper (saldo + nilai posisi − modal awal)</div>
        <div class="value" id="hero-value">–</div>
      </div>
      <div class="sub" id="hero-sub"></div>
    </div>
  </section>

  <div class="tiles" id="tiles"></div>

  <section class="card">
    <h2>P&amp;L terealisasi kumulatif (SOL)</h2>
    <div class="chart-box" id="chart-box"><div class="tip" id="tip"></div></div>
  </section>

  <section class="card">
    <h2>Posisi terbuka</h2>
    <div class="table-wrap" id="positions"></div>
  </section>

  <section class="card">
    <h2>Token migrated yang sedang dipantau</h2>
    <div class="sub" style="margin:-4px 0 8px;font-size:12px" id="watch-note"></div>
    <div class="table-wrap" id="tokens"></div>
  </section>

  <section class="card">
    <h2>Riset: token yang lolos filter vs yang ditolak</h2>
    <div class="sub" style="font-size:12px;margin-bottom:8px">Median kenaikan harga dari titik pembanding (awal jendela beli, sama untuk semua token), dan persentase token yang naik. Di mode beli saat dip, nilai strategi dari P&amp;L dan diagnosa posisi di laporan.</div>
    <div class="table-wrap" id="research"></div>
    <ul class="verdict" id="verdict"></ul>
  </section>

  <div class="grid2">
    <section class="card">
      <h2>Aktivitas terbaru</h2>
      <ul class="activity" id="activity"></ul>
    </section>
    <section class="card">
      <h2>Alasan ditolak terbanyak</h2>
      <div class="table-wrap" id="reasons"></div>
    </section>
  </div>
  <div style="height:14px"></div>

  <section class="card">
    <h2>Transaksi terakhir</h2>
    <div class="table-wrap" id="trades"></div>
  </section>

  <section class="card">
    <h2>Sumber data</h2>
    <div class="feeds" id="feeds"></div>
  </section>

  <footer>Hanya-baca. Semua transaksi adalah simulasi (paper); tidak ada wallet atau uang sungguhan.</footer>
</div>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "href") node.href = v;
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  return node;
}
const num = (v) => (v === null || v === undefined || v === "" || isNaN(Number(v))) ? null : Number(v);
function fmtSol(v, d = 4, sign = true) { v = num(v); if (v === null) return "–"; return (sign && v > 0 ? "+" : "") + v.toFixed(d) + " SOL"; }
function fmtPct(v, d = 0, sign = true) { v = num(v); if (v === null) return "–"; return (sign && v > 0 ? "+" : "") + v.toFixed(d) + "%"; }
function fmtUsd(v) {
  v = num(v); if (v === null) return "–";
  const a = Math.abs(v), s = v < 0 ? "-" : "";
  if (a >= 1e6) return s + "$" + (a / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return s + "$" + (a / 1e3).toFixed(1) + "k";
  return s + "$" + a.toFixed(0);
}
function fmtAge(sec) { sec = num(sec); if (sec === null) return "–"; if (sec < 90) return Math.round(sec) + " dtk"; if (sec < 5400) return Math.round(sec / 60) + " mnt"; return (sec / 3600).toFixed(1) + " jam"; }
function fmtTime(ts) { if (!ts) return "–"; const d = new Date(ts * 1000); return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
function fmtUtc(s) { if (!s) return "–"; const d = new Date(s.replace(" ", "T") + "Z"); return isNaN(d) ? s : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
function signClass(v) { v = num(v); return v === null || v === 0 ? "" : (v > 0 ? "up" : "down"); }
function gmgnLink(mint, label) {
  return el("a", { href: "https://gmgn.ai/sol/token/" + encodeURIComponent(mint), target: "_blank", rel: "noopener noreferrer", title: mint, text: label });
}
function statusBadge(kind, label) {
  const map = { good: ["var(--good)", "✓"], warning: ["var(--warning)", "!"], serious: ["var(--serious)", "!"], critical: ["var(--critical)", "✕"], neutral: ["var(--axis)", "•"] };
  const [color, icon] = map[kind] || map.neutral;
  return el("span", { class: "status" }, el("span", { class: "dot", style: "background:" + color }), icon + " " + label);
}
function table(headers, rows, emptyText) {
  if (!rows.length) return el("div", { class: "empty", text: emptyText });
  return el("table", {},
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", { class: h.num ? "num" : "", text: h.label })))),
    el("tbody", {}, rows));
}
function replace(id, node) { const box = $(id); box.replaceChildren(node); }

// ---------------------------------------------------------------- theme
(function () {
  let saved = null;
  try { saved = localStorage.getItem("migbot-theme"); } catch (e) {}
  if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
  $("theme-btn").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme")
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("migbot-theme", next); } catch (e) {}
    if (lastTimeline) drawChart(lastTimeline);
  });
})();

// ---------------------------------------------------------------- status
const STATUS_KIND = { "dipantau": ["neutral", "dipantau"], "dibeli": ["good", "dibeli"], "lolos, tak dibeli": ["warning", "lolos, tak dibeli"],
  "ditolak": ["critical", "ditolak"], "tanpa data": ["warning", "tanpa data"] };
let lastStatus = null, lastReport = null;
function renderStatus(st) {
  lastStatus = st;
  if (lastReport) renderReasons();
  if (st.missing) {
    $("run-dot").style.background = "var(--warning)"; $("run-text").textContent = "belum ada data";
    const b = $("banner"); b.hidden = false;
    b.textContent = "File status belum ada. Jalankan bot dulu: python -m migbot run";
    return;
  }
  const running = st.running && !st.stale;
  $("run-dot").style.background = running ? "var(--good)" : "var(--critical)";
  $("run-text").textContent = running ? "✓ bot jalan" : (st.running ? "! tidak ada kabar" : "✕ bot berhenti");
  $("updated").textContent = "diperbarui " + fmtTime(st.updated_at);
  const b = $("banner");
  if (!running) {
    b.hidden = false;
    b.textContent = st.running ? "Bot tidak memperbarui status lebih dari 30 detik. Cek: sudo systemctl status migbot"
                               : "Bot sedang berhenti. Angka di bawah adalah keadaan terakhir.";
  } else if (st.risk && st.risk.halted) {
    b.hidden = false;
    b.textContent = "Batas rugi harian tercapai: bot tidak membeli token baru sampai 00:00 UTC.";
  } else { b.hidden = true; }

  const pnl = num(st.equity_sol) - num(st.start_balance_sol);
  const hv = $("hero-value");
  hv.textContent = fmtSol(pnl, 4); hv.className = "value " + signClass(pnl);
  $("hero-sub").textContent = "Saldo " + fmtSol(st.balance_sol, 4, false) + " · equity " + fmtSol(st.equity_sol, 4, false)
    + " · modal awal " + fmtSol(st.start_balance_sol, 2, false);

  const t = st.today || {}, r = st.risk || {};
  const tiles = [
    ["Migrasi hari ini", String(t.migrations ?? 0), (t.late ? t.late + " terlambat ditemukan · " : "") + "hari UTC"],
    ["Dibeli / ditolak", (t.bought ?? 0) + " / " + (t.rejected ?? 0), "hari ini"],
    ["Posisi terbuka", (st.positions || []).length + " / " + (r.max_open_positions ?? "?"), "beli " + fmtSol(st.settings && st.settings.buy_sol, 2, false) + " per token"],
    ["P&L hari ini", fmtSol(r.realized_today, 4), "terealisasi, batas rugi " + fmtSol(r.max_daily_loss_sol, 2, false)],
    ["Posisi ditutup", String(t.closed ?? 0), (t.wins ?? 0) + " untung hari ini"],
  ];
  $("tiles").replaceChildren(...tiles.map(([label, value, note], i) =>
    el("div", { class: "tile" }, el("div", { class: "label", text: label }),
      el("div", { class: "value " + (i === 3 ? signClass(r.realized_today) : ""), text: value }),
      el("div", { class: "note", text: note }))));

  // positions
  const posRows = (st.positions || []).map((p) => el("tr", {},
    el("td", {}, gmgnLink(p.mint, "$" + p.symbol)),
    el("td", { class: "num", text: fmtAge(st.updated_at - p.opened_at) }),
    el("td", { class: "num", text: fmtSol(p.cost_sol, 4, false) }),
    el("td", { class: "num", text: fmtSol(p.value_sol, 4, false) }),
    el("td", { class: "num " + signClass(p.pnl_sol), text: fmtSol(p.pnl_sol, 4) + " (" + fmtPct(p.pnl_pct) + ")" }),
    el("td", { class: "num " + signClass(p.change_pct), text: fmtPct(p.change_pct) }),
    el("td", { class: "num", text: fmtUsd(p.last_mcap_usd) }),
    el("td", { class: "num", text: fmtUsd(p.last_liquidity_usd) }),
    el("td", { text: (p.tp_done || []).length ? (p.tp_done.length + " TP") : "–" }),
    el("td", { class: "muted", text: p.method }),
  ));
  replace("positions", table([{ label: "Token" }, { label: "Umur", num: 1 }, { label: "Modal", num: 1 }, { label: "Nilai", num: 1 },
    { label: "P&L", num: 1 }, { label: "Harga", num: 1 }, { label: "Mcap", num: 1 }, { label: "Likuiditas", num: 1 },
    { label: "Take profit" }, { label: "Harga dari" }], posRows, "Belum ada posisi terbuka."));

  // tokens
  const s = st.settings || {};
  $("watch-note").textContent = "Beli dipertimbangkan " + fmtAge(s.entry_delay_s) + " sampai " + fmtAge(s.entry_window_s)
    + " setelah migrasi; setiap token dipantau " + (s.track_minutes || "?") + " menit untuk riset.";
  const tokRows = (st.tokens || []).map((tk) => {
    const last = tk.last || {};
    const [kind, label] = STATUS_KIND[tk.status] || ["neutral", tk.status];
    const buys = num(last.buys_m5), sells = num(last.sells_m5);
    const ratio = buys !== null && sells !== null && buys + sells > 0 ? buys / (buys + sells) * 100 : null;
    const checks = tk.checks || [];
    let detail;
    if (checks.length) {
      const icons = { "ok": "✓", "gagal": "✕", "lewati": "–" };
      detail = el("details", {}, el("summary", { text: (tk.reasons && tk.reasons.length) ? tk.reasons.slice(0, 2).join("; ") : "semua cek lolos" }),
        el("ul", { class: "checks" }, checks.map((c) => el("li", {},
          el("span", { class: "ic " + (c.status === "gagal" ? "down" : c.status === "ok" ? "up" : "muted"), text: icons[c.status] || "•" }),
          el("span", {}, c.name + ": " + c.detail)))));
    } else {
      detail = el("span", { class: "muted", text: (tk.reasons && tk.reasons.length) ? tk.reasons.join("; ") : (tk.age_s < (s.entry_delay_s || 0) ? "menunggu jeda beli" : "menunggu data") });
    }
    return el("tr", {},
      el("td", {}, gmgnLink(tk.mint, tk.label || tk.mint.slice(0, 6))),
      el("td", {}, statusBadge(kind, label)),
      el("td", { class: "num", text: fmtAge(tk.age_s) }),
      el("td", { class: "num", text: fmtUsd(last.market_cap_usd) }),
      el("td", { class: "num", text: fmtUsd(last.liquidity_usd) }),
      el("td", { class: "num", text: fmtUsd(last.volume_m5) }),
      el("td", { class: "num", text: ratio === null ? "–" : ratio.toFixed(0) + "%" }),
      el("td", { class: "num " + signClass(tk.change_pct), text: fmtPct(tk.change_pct) }),
      el("td", { class: "wrap-cell" }, detail));
  });
  replace("tokens", table([{ label: "Token" }, { label: "Status" }, { label: "Umur", num: 1 }, { label: "Mcap", num: 1 },
    { label: "Likuiditas", num: 1 }, { label: "Vol 5m", num: 1 }, { label: "Beli 5m", num: 1 }, { label: "Dari puncak", num: 1 },
    { label: "Filter" }], tokRows, "Belum ada migrasi yang terdeteksi. Migrasi pump.fun biasanya muncul beberapa kali per jam."));

  // activity
  const acts = (st.recent || []).slice(0, 40).map((a) => el("li", {},
    el("time", { text: fmtTime(a.t) }), el("span", { text: a.text })));
  $("activity").replaceChildren(...(acts.length ? acts : [el("li", { class: "muted", text: "Belum ada aktivitas." })]));

  // feeds
  const feeds = (st.feeds || []).map((f) => {
    let kind = "good", detail;
    if (f.name === "PumpPortal") {
      kind = f.connected ? "good" : "critical";
      detail = (f.connected ? "tersambung" : "terputus") + " · " + (f.event_count || 0) + " migrasi"
        + (f.last_event_at ? " · terakhir " + fmtTime(f.last_event_at) : "") + (f.reconnects ? " · sambung ulang " + f.reconnects + "x" : "")
        + (f.last_error && !f.connected ? " · " + f.last_error : "");
    } else {
      const fresh = f.last_ok && (st.updated_at - f.last_ok) < 600;
      if (f.note) kind = "warning";
      else if (f.consecutive_errors >= 3) kind = "critical";
      else if (f.consecutive_errors > 0 || !fresh) kind = f.ok_count ? "warning" : "neutral";
      detail = (f.ok_count || 0) + " ok · " + (f.error_count || 0) + " gagal"
        + (f.last_ok ? " · terakhir ok " + fmtTime(f.last_ok) : "")
        + (f.note ? " · " + f.note : (f.consecutive_errors && f.last_error ? " · " + f.last_error : ""));
    }
    const label = { good: "OK", warning: "perhatian", critical: "gagal", neutral: "belum dipakai" }[kind];
    return el("div", { class: "feed" }, el("div", {},
      el("div", { class: "name" }, f.name + " "), statusBadge(kind, label), el("div", { class: "detail", text: detail })));
  });
  const tg = st.telegram || {};
  feeds.push(el("div", { class: "feed" }, el("div", {}, el("div", { class: "name", text: "Telegram" }),
    statusBadge(tg.enabled ? (tg.last_error ? "warning" : "good") : "neutral", tg.enabled ? (tg.last_error ? "perhatian" : "OK") : "tidak aktif"),
    el("div", { class: "detail", text: tg.enabled ? (tg.sent || 0) + " pesan terkirim" + (tg.last_error ? " · " + tg.last_error : "") : "isi TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID di .env" }))));
  $("feeds").replaceChildren(...feeds);
}

// ---------------------------------------------------------------- report
function renderReport(rep) {
  lastReport = rep;
  const cols = rep.ret_columns || [];
  const groups = rep.research || {};
  const rows = !rep.tokens_total ? [] : Object.entries(groups).map(([name, g]) => el("tr", {},
    el("td", { style: "font-weight:600", text: name }),
    el("td", { class: "num", text: String(g.n) }),
    cols.map((c) => {
      const st = (g.columns || {})[c] || {};
      return el("td", { class: "num " + signClass(st.median), text: st.n ? fmtPct(st.median, 1) + " (" + Math.round(st.pct_up) + "% naik)" : "–" });
    }),
    el("td", { class: "num", text: g.pct_2x === null || g.pct_2x === undefined ? "–" : Math.round(g.pct_2x) + "%" }),
    el("td", { class: "num", text: g.pct_halved === null || g.pct_halved === undefined ? "–" : Math.round(g.pct_halved) + "%" })));
  replace("research", table([{ label: "Kelompok" }, { label: "Token", num: 1 }, ...cols.map((c) => ({ label: c.slice(4) + " ", num: 1 })),
    { label: "Pernah 2x", num: 1 }, { label: "Pernah −50%", num: 1 }], rows,
    "Belum ada token yang selesai dipantau. Setiap token masuk tabel ini setelah " + ((lastStatus && lastStatus.settings && lastStatus.settings.track_minutes) || 120) + " menit dipantau."));
  $("verdict").replaceChildren(...(rep.verdict || []).map((v) => el("li", { text: v })));

  renderReasons();

  const trades = (rep.recent_trades || []).map((t) => el("tr", {},
    el("td", { class: "num", text: fmtUtc(t.time_utc) }),
    el("td", {}, gmgnLink(t.mint, "$" + (t.symbol || "?"))),
    el("td", { text: t.side === "BUY" ? "BELI" : "JUAL" }),
    el("td", { text: t.reason }),
    el("td", { class: "num", text: fmtSol(t.sol, 4, false) }),
    el("td", { class: "num " + signClass(t.pnl_sol), text: t.side === "SELL" ? fmtSol(t.pnl_sol, 4) + " (" + fmtPct(t.pnl_pct) + ")" : "–" }),
    el("td", { class: "num", text: fmtUsd(t.mcap_usd) }),
    el("td", { class: "muted", text: t.method })));
  replace("trades", table([{ label: "Waktu", num: 1 }, { label: "Token" }, { label: "Sisi" }, { label: "Alasan" }, { label: "SOL", num: 1 },
    { label: "P&L", num: 1 }, { label: "Mcap", num: 1 }, { label: "Harga dari" }], trades, "Belum ada transaksi."));

  lastTimeline = (rep.pnl && rep.pnl.timeline) || [];
  drawChart(lastTimeline);
}

function renderReasons() {
  const counts = Object.assign({}, (lastReport && lastReport.reject_reasons) || {});
  for (const tk of (lastStatus && lastStatus.tokens) || []) {
    if (tk.status !== "ditolak") continue;
    for (const r of tk.reasons || []) { const k = r.split(":")[0].trim(); counts[k] = (counts[k] || 0) + 1; }
  }
  const rows = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 12)
    .map(([k, v]) => el("tr", {}, el("td", { text: k }), el("td", { class: "num", text: String(v) })));
  replace("reasons", table([{ label: "Cek yang gagal" }, { label: "Token", num: 1 }], rows, "Belum ada token yang ditolak."));
}

// ---------------------------------------------------------------- chart
let lastTimeline = null;
function niceStep(range, target) {
  const raw = range / Math.max(1, target), mag = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / mag;
  return (n >= 5 ? 10 : n >= 2 ? 5 : n >= 1 ? 2 : 1) * mag;
}
function drawChart(points) {
  const box = $("chart-box"), tip = $("tip");
  box.querySelectorAll("svg, .empty").forEach((n) => n.remove());
  if (!points.length) { box.appendChild(el("div", { class: "empty", text: "Belum ada posisi yang dijual." })); return; }
  const css = getComputedStyle(document.documentElement);
  const col = (name) => css.getPropertyValue(name).trim();
  const W = Math.max(300, box.clientWidth), H = 240, m = { l: 58, r: 72, t: 12, b: 28 };
  const data = [{ t: null, pnl: 0 }, ...points];
  const ys = data.map((d) => d.pnl);
  let lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  if (hi - lo < 1e-9) { hi += 0.01; lo -= 0.01; }
  const step = niceStep(hi - lo, 4);
  lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
  const x = (i) => m.l + (W - m.l - m.r) * (data.length === 1 ? 0 : i / (data.length - 1));
  const y = (v) => m.t + (H - m.t - m.b) * (1 - (v - lo) / (hi - lo));
  const NS = "http://www.w3.org/2000/svg";
  const s = (tag, attrs) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, tabindex: "0", role: "img",
    "aria-label": "P&L terealisasi kumulatif, terakhir " + fmtSol(ys[ys.length - 1]) });
  const decimals = step >= 1 ? 0 : Math.min(4, Math.ceil(-Math.log10(step)));
  for (let v = lo; v <= hi + step / 2; v += step) {
    const yy = y(v);
    svg.appendChild(s("line", { x1: m.l, x2: W - m.r, y1: yy, y2: yy, stroke: Math.abs(v) < step / 2 ? col("--axis") : col("--grid"), "stroke-width": 1 }));
    const label = s("text", { x: m.l - 8, y: yy + 4, "text-anchor": "end", "font-size": 11, fill: col("--muted") });
    label.textContent = (v > 0 ? "+" : "") + v.toFixed(decimals);
    svg.appendChild(label);
  }
  const first = s("text", { x: m.l, y: H - 8, "font-size": 11, fill: col("--muted") }); first.textContent = "awal"; svg.appendChild(first);
  const lastT = s("text", { x: W - m.r, y: H - 8, "font-size": 11, fill: col("--muted"), "text-anchor": "end" });
  lastT.textContent = fmtUtc(data[data.length - 1].t); svg.appendChild(lastT);
  const line = data.map((d, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(d.pnl).toFixed(1)).join(" ");
  svg.appendChild(s("path", { d: line + ` L ${x(data.length - 1).toFixed(1)} ${y(0).toFixed(1)} L ${x(0).toFixed(1)} ${y(0).toFixed(1)} Z`, fill: col("--wash-1"), stroke: "none" }));
  svg.appendChild(s("path", { d: line, fill: "none", stroke: col("--series-1"), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  const li = data.length - 1;
  svg.appendChild(s("circle", { cx: x(li), cy: y(ys[li]), r: 4, fill: col("--series-1"), stroke: col("--surface"), "stroke-width": 2 }));
  const endLabel = s("text", { x: x(li) + 8, y: y(ys[li]) + 4, "font-size": 12, "font-weight": 600, fill: col("--ink") });
  endLabel.textContent = (ys[li] > 0 ? "+" : "") + ys[li].toFixed(4); svg.appendChild(endLabel);
  const cross = s("line", { x1: 0, x2: 0, y1: m.t, y2: H - m.b, stroke: col("--axis"), "stroke-width": 1, visibility: "hidden" });
  const focus = s("circle", { r: 5, fill: col("--series-1"), stroke: col("--surface"), "stroke-width": 2, visibility: "hidden" });
  svg.appendChild(cross); svg.appendChild(focus);
  const hit = s("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" });
  svg.appendChild(hit);
  let idx = li;
  function show(i) {
    idx = Math.max(0, Math.min(li, i));
    const px = x(idx), py = y(ys[idx]);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    focus.setAttribute("cx", px); focus.setAttribute("cy", py); focus.setAttribute("visibility", "visible");
    tip.replaceChildren(el("strong", { text: fmtSol(ys[idx]) }), el("span", { class: "sub", text: idx === 0 ? "awal" : fmtUtc(data[idx].t) + " · jual ke-" + idx }));
    tip.style.display = "block";
    const scale = box.clientWidth / W;
    const left = Math.min(Math.max(0, px * scale + 12), box.clientWidth - tip.offsetWidth);
    tip.style.left = left + "px"; tip.style.top = Math.max(0, py * scale - 44) + "px";
  }
  function hide() { cross.setAttribute("visibility", "hidden"); focus.setAttribute("visibility", "hidden"); tip.style.display = "none"; }
  hit.addEventListener("pointermove", (ev) => {
    const rect = svg.getBoundingClientRect(); const px = (ev.clientX - rect.left) * (W / rect.width);
    show(Math.round((px - m.l) / ((W - m.l - m.r) / Math.max(1, li))));
  });
  hit.addEventListener("pointerdown", (ev) => hit.dispatchEvent(new PointerEvent("pointermove", ev)));
  hit.addEventListener("pointerleave", hide);
  svg.addEventListener("focus", () => show(idx));
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowLeft") { show(idx - 1); ev.preventDefault(); }
    if (ev.key === "ArrowRight") { show(idx + 1); ev.preventDefault(); }
  });
  box.appendChild(svg);
}
let resizeTimer = null;
addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => lastTimeline && drawChart(lastTimeline), 150); });

// ---------------------------------------------------------------- polling
async function getJson(url) { const r = await fetch(url, { cache: "no-store" }); if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }
async function pollStatus() {
  try { renderStatus(await getJson("api/status")); document.body.style.opacity = 1; }
  catch (e) { $("run-dot").style.background = "var(--critical)"; $("run-text").textContent = "✕ dashboard tidak tersambung"; document.body.style.opacity = 0.7; }
}
async function pollReport() { try { renderReport(await getJson("api/report")); } catch (e) { /* keep the previous render */ } }
pollStatus(); pollReport();
setInterval(pollStatus, 5000);
setInterval(pollReport, 30000);
</script>
</body>
</html>
"""
