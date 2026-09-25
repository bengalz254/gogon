"""Live monitoring dashboard for the Up/Down engine (local, read-only).

Run it in a second terminal while the bot runs:

    python scripts/updown_dashboard.py
    python scripts/updown_dashboard.py --host 0.0.0.0     # also reachable from your phone on the same network

It reads data/updown_status.json (written by `python -m bot.updown` every
second) and the journals in data/, and serves a page at
http://127.0.0.1:8766 that refreshes itself. Nothing is sent anywhere, it
never touches the bot, and it can't place or cancel orders.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bot.updown.analysis import (  # noqa: E402
    calibration,
    load_settlements,
    pnl_by_strategy,
    pnl_timeline,
    rule_check,
    tail_csv,
)


class DataSource:
    """Reads the bot's files; caches the heavier summaries."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._last_status = None
        self._summary = None
        self._summary_at = 0.0
        self._calibration = None
        self._calibration_at = 0.0
        self._calibration_key = None
        self._lock = threading.Lock()

    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, name)

    def live(self) -> dict:
        try:
            with open(self._path("updown_status.json"), encoding="utf-8") as f:
                self._last_status = json.load(f)
        except FileNotFoundError:
            self._last_status = None
        except (OSError, ValueError):
            pass  # mid-write on some filesystems: keep the previous snapshot
        status = self._last_status
        now = time.time()
        return {"status": status, "age": (now - status["ts"]) if status else None, "now": now}

    def summary(self) -> dict:
        with self._lock:
            now = time.time()
            if self._summary is not None and now - self._summary_at < 10:
                return self._summary
            settlements = self._path("updown_settlements.csv")
            outcomes, rows = load_settlements(settlements)
            # Calibration re-reads the whole snapshot journal: only redo it when a
            # new result arrived, and at most every 30s.
            try:
                key = os.path.getmtime(settlements)
            except OSError:
                key = None
            if self._calibration is None or (key != self._calibration_key and now - self._calibration_at > 30):
                self._calibration = calibration(self._path("updown_snapshots.jsonl"), outcomes)
                self._calibration_at = now
                self._calibration_key = key
            trades = [
                {k: r.get(k, "") for k in ("timestamp", "mode", "strategy", "market_id", "outcome", "side",
                                           "price", "size_shares", "size_usd", "reason")}
                for r in tail_csv(self._path("trades.csv"), 40)
            ]
            for t in trades:
                t["reason"] = (t.get("reason") or "")[:240]
            self._summary = {
                "generated": now,
                "timeline": pnl_timeline(rows)[-600:],
                "by_strategy": pnl_by_strategy(rows),
                "calibration": self._calibration,
                "rules": rule_check(rows),
                "trades": trades,
            }
            self._summary_at = now
            return self._summary


INDEX_HTML = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pantau Up/Down</title>
<link rel="icon" href="data:,">
<style>
  :root {
    color-scheme: light;
    --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
    --model: #2a78d6; --model-wash: rgba(42,120,214,0.12); --market: #898781;
    --meter-track: #cde2fb; --neg: #e34948;
    --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
    --good-ink: #006300;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
      --model: #3987e5; --model-wash: rgba(57,135,229,0.16); --market: #898781;
      --meter-track: #0d366b; --neg: #e66767; --good-ink: #0ca30c;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --model: #3987e5; --model-wash: rgba(57,135,229,0.16); --market: #898781;
    --meter-track: #0d366b; --neg: #e66767; --good-ink: #0ca30c;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--page); color: var(--ink);
         font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 1400px; margin: 0 auto; padding: 20px 16px 48px; }
  header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 16px; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  h1 span { color: var(--ink-2); font-weight: 400; }
  h2 { font-size: 12px; font-weight: 600; color: var(--ink-2); text-transform: uppercase; letter-spacing: .04em; margin: 0 0 12px; }
  .row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .muted { color: var(--muted); }
  .ink2 { color: var(--ink-2); }
  .num { font-variant-numeric: tabular-nums; }
  button.plain { background: none; border: 1px solid var(--border); color: var(--ink-2); border-radius: 8px;
                 padding: 4px 10px; font: inherit; font-size: 12px; cursor: pointer; }
  button.plain:hover { color: var(--ink); }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; min-width: 0; }
  .banner { border-radius: 10px; padding: 10px 14px; margin-bottom: 16px; background: var(--surface);
            border: 1px solid var(--border); display: none; }
  .banner.show { display: block; }
  .status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-2); }
  .status .icon { width: 16px; height: 16px; border-radius: 50%; display: inline-grid; place-items: center;
                  color: #fff; font-size: 10px; font-weight: 700; flex: none; }
  .s-good .icon { background: var(--good); } .s-warning .icon { background: var(--warning); color: #0b0b0b; }
  .s-serious .icon { background: var(--serious); color: #0b0b0b; } .s-critical .icon { background: var(--critical); }
  .s-neutral .icon { background: var(--axis); color: var(--ink); }
  .pill { font-size: 11px; font-weight: 600; letter-spacing: .03em; padding: 2px 8px; border-radius: 999px;
          border: 1px solid var(--border); color: var(--ink-2); }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 16px; }
  .kpi .label { font-size: 12px; color: var(--ink-2); margin-bottom: 4px; }
  .kpi .value { font-size: 24px; font-weight: 600; }
  .kpi .sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
  .up { color: var(--good-ink); } .down { color: var(--critical); }
  .meter { height: 8px; border-radius: 4px; background: var(--meter-track); overflow: hidden; margin-top: 8px; position: relative; }
  .meter > i { display: block; height: 100%; background: var(--model); border-radius: 0 4px 4px 0; }
  .coins { display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); gap: 12px; margin-bottom: 16px; }
  .coin h3 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: .02em; }
  .coin .line { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; margin-top: 8px; }
  .coin .small { font-size: 12px; color: var(--ink-2); margin-top: 6px; }
  .winbar { position: relative; height: 8px; border-radius: 4px; background: var(--meter-track); margin-top: 8px; }
  .winbar > i { position: absolute; left: 0; top: 0; bottom: 0; background: var(--model); border-radius: 4px 0 0 4px; }
  .winbar > b { position: absolute; top: -3px; bottom: -3px; width: 2px; background: var(--surface); }
  .winbar-labels { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-top: 3px; }
  .key { display: inline-block; vertical-align: middle; margin-right: 4px; }
  .key.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--model); }
  .key.tick { width: 3px; height: 12px; border-radius: 2px; background: var(--market); }
  .key.line { width: 14px; height: 2px; border-radius: 1px; }
  .pos { font-size: 12px; margin-top: 8px; border-top: 1px solid var(--grid); padding-top: 8px; }
  .pos div { display: flex; justify-content: space-between; gap: 8px; }
  .dim { opacity: .45; transition: opacity .3s; }
  .grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 12px; margin-bottom: 16px; }
  @media (max-width: 520px) { .grid2 { grid-template-columns: 1fr; } }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th, td { text-align: left; padding: 6px 6px; border-bottom: 1px solid var(--grid); vertical-align: top; }
  th { color: var(--ink-2); font-weight: 500; font-size: 12px; }
  td.r, th.r { text-align: right; font-variant-numeric: tabular-nums; }
  .scroll { overflow-x: auto; }
  .tablewrap { max-height: 360px; overflow: auto; }
  .reason { color: var(--ink-2); max-width: 420px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bars { display: flex; flex-direction: column; gap: 12px; }
  .bar-row { display: grid; grid-template-columns: 130px 1fr; gap: 10px; align-items: center; }
  .bar-name { font-size: 13px; }
  .bar-name small { display: block; color: var(--muted); font-size: 11px; }
  .bar-track { position: relative; height: 22px; }
  .bar-zero { position: absolute; top: -2px; bottom: -2px; width: 1px; background: var(--axis); }
  .bar { position: absolute; top: 3px; height: 16px; }
  .bar.pos { background: var(--model); border-radius: 0 4px 4px 0; }
  .bar.neg { background: var(--neg); border-radius: 4px 0 0 4px; }
  .bar-val { position: absolute; top: 2px; font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  svg text { fill: var(--muted); font: 11px system-ui, -apple-system, "Segoe UI", sans-serif; }
  #tooltip { position: fixed; pointer-events: none; z-index: 10; background: var(--surface); color: var(--ink);
             border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px;
             box-shadow: 0 4px 16px rgba(0,0,0,.18); display: none; min-width: 140px; }
  #tooltip .v { font-weight: 600; font-variant-numeric: tabular-nums; }
  #tooltip .t { color: var(--ink-2); margin-bottom: 4px; }
  .empty { color: var(--muted); font-size: 13px; padding: 18px 0; text-align: center; }
  footer { color: var(--muted); font-size: 12px; text-align: center; margin-top: 24px; }
  @media (max-width: 600px) { .hide-sm { display: none; } }
</style>
</head>
<body>
<main>
  <header>
    <h1>Up/Down Bot <span>&mdash; Pantau</span></h1>
    <div class="row">
      <span id="conn" class="status s-neutral"><span class="icon">·</span><span>memuat…</span></span>
      <span id="mode" class="pill">&ndash;</span>
      <button class="plain" id="theme" title="Ganti tema terang/gelap">tema</button>
    </div>
  </header>
  <div id="banner" class="banner"></div>

  <section class="kpis" id="kpis"></section>

  <h2>Window live per koin</h2>
  <section class="coins" id="coins"></section>

  <section class="grid2">
    <div class="card">
      <div class="row" style="justify-content: space-between; margin-bottom: 8px">
        <h2 style="margin: 0">P&amp;L kumulatif (window yang sudah selesai)</h2>
        <button class="plain" id="pnl-toggle">tabel</button>
      </div>
      <div id="pnl-chart"></div>
      <div id="pnl-table" class="tablewrap" hidden></div>
    </div>
    <div class="card">
      <h2>P&amp;L per strategi</h2>
      <div id="strategies" class="bars"></div>
    </div>
  </section>

  <section class="grid2">
    <div class="card">
      <h2>Kesehatan feed</h2>
      <div class="scroll" id="feeds"></div>
    </div>
    <div class="card">
      <h2>Model vs pasar (kalibrasi)</h2>
      <div id="calibration"></div>
    </div>
  </section>

  <div class="card">
    <h2>Aktivitas terbaru (data/trades.csv)</h2>
    <div class="scroll tablewrap" id="trades"></div>
  </div>
  <footer>Refresh otomatis setiap detik &middot; hanya membaca file di folder data/ &middot; tidak bisa mengirim order</footer>
</main>
<div id="tooltip"></div>

<script>
"use strict";
const NS = "http://www.w3.org/2000/svg";
const state = { live: null, summary: null, pnlTable: false };
// Coin cards re-render every second; remember what the pointer is over so the
// crosshair and tooltip survive the refresh.
const hover = { id: null, x: 0, y: 0 };

// ---------- tiny DOM helpers (strings always go in as text, never HTML) ----------
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k === "style") e.setAttribute("style", v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}
function sv(tag, attrs, ...kids) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) e.setAttribute(k, v);
  for (const c of kids.flat()) if (c) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}
function fill(id, ...kids) { const n = document.getElementById(id); n.replaceChildren(...kids.flat().filter(Boolean)); return n; }

// ---------- formatting ----------
const usd = (n, sign = false) => n === null || n === undefined ? "–" :
  (sign && n > 0 ? "+" : n < 0 ? "−" : "") + "$" + Math.abs(n).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const p2 = (x) => x === null || x === undefined ? "–" : x.toFixed(2);
const pct = (x, d = 2) => x === null || x === undefined ? "–" : (x > 0 ? "+" : x < 0 ? "−" : "") + Math.abs(x * 100).toFixed(d) + "%";
const secs = (s) => { if (s === null || s === undefined) return "–"; s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("id-ID", {hour: "2-digit", minute: "2-digit", second: "2-digit"});
const hm = (ts) => new Date(ts * 1000).toLocaleTimeString("id-ID", {hour: "2-digit", minute: "2-digit"});
const ago = (s) => s < 90 ? Math.round(s) + " detik" : s < 5400 ? Math.round(s / 60) + " menit" : Math.round(s / 3600) + " jam";
const price = (x) => x === null || x === undefined ? "–" :
  x >= 100 ? x.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2}) : x >= 1 ? x.toFixed(4) : x.toPrecision(5);

function status(kind, label, title) {
  const icons = {good: "✓", warning: "!", serious: "!", critical: "✕", neutral: "·"};
  return el("span", {class: "status s-" + kind, title: title || null}, el("span", {class: "icon", text: icons[kind]}), el("span", {text: label}));
}

// ---------- tooltip ----------
const tip = document.getElementById("tooltip");
function showTip(evt, title, rows) {
  tip.replaceChildren(el("div", {class: "t", text: title}), ...rows.map(([key, label, value]) =>
    el("div", {class: "row", style: "justify-content: space-between; gap: 12px"},
      el("span", {class: "ink2"}, key, label), el("span", {class: "v", text: value}))));
  tip.style.display = "block";
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = evt.clientX + 14, y = evt.clientY + 14;
  if (x + w > innerWidth - 8) x = evt.clientX - w - 14;
  if (y + h > innerHeight - 8) y = evt.clientY - h - 14;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
function hideTip() { tip.style.display = "none"; }
const lineKey = (color) => el("span", {class: "key line", style: "background:" + color});

// ---------- header / banner / KPIs ----------
function renderHeader(live) {
  const st = live && live.status;
  const conn = document.getElementById("conn");
  let kind = "critical", label = "bot tidak berjalan";
  if (st && live.age < 5) { kind = "good"; label = "online · " + clock(st.ts); }
  else if (st && live.age < 30) { kind = "warning"; label = "terlambat " + ago(live.age); }
  else if (st) { label = "offline · update terakhir " + ago(live.age) + " lalu"; }
  conn.replaceWith(Object.assign(status(kind, label), {id: "conn"}));
  document.getElementById("mode").textContent = st ? st.mode.toUpperCase() : "–";
  const banner = document.getElementById("banner");
  const offline = !st || live.age >= 30;
  banner.classList.toggle("show", offline);
  banner.replaceChildren(offline ? el("span", {},
    status("critical", st ? "Data berhenti diperbarui " + ago(live.age) + " lalu." : "Belum ada data dari bot."),
    " Jalankan ", el("code", {text: "python -m bot.updown"}), " di jendela lain; halaman ini akan hidup sendiri.") : "");
  document.getElementById("coins").classList.toggle("dim", offline);
}

function kpi(label, value, sub, cls, extra) {
  return el("div", {class: "card kpi"}, el("div", {class: "label", text: label}),
    el("div", {class: "value " + (cls || ""), text: value}), sub ? el("div", {class: "sub", text: sub}) : null, extra || null);
}

function renderKpis(st, summary) {
  if (!st) { fill("kpis", el("div", {class: "empty", text: "Menunggu data…"})); return; }
  const r = st.risk, ex = st.exposure;
  const live = st.windows.filter(w => w.phase === "live");
  const tradable = live.filter(w => w.tradable).length;
  const exPct = ex.cap ? Math.min(1, ex.total / ex.cap) : 0;
  const meter = el("div", {class: "meter", title: "eksposur / batas"}, el("i", {style: "width:" + (exPct * 100).toFixed(1) + "%"}));
  const cooldowns = Object.entries(r.cooldowns || {});
  let guard = status("good", "aman");
  if (r.kill_switch) guard = status("critical", "kill switch AKTIF");
  else if (cooldowns.length) guard = status("warning", cooldowns.map(([k, s]) => k + " jeda " + secs(s)).join(", "));
  const total = summary ? summary.timeline.length : 0;
  const cls = (v) => v > 0 ? "up" : v < 0 ? "down" : "";
  fill("kpis",
    kpi("P&L hari ini (UTC)", usd(r.realized_today, true), "pending " + usd(r.pending, true), cls(r.realized_today)),
    kpi("P&L sejak bot start", usd(r.realized_total, true), "bankroll " + usd(r.bankroll), cls(r.realized_total)),
    kpi("Eksposur", usd(ex.total), "batas " + usd(ex.cap), "", meter),
    kpi("Window live", String(live.length), tradable + " bisa trading"),
    kpi("Aktivitas", st.stats.fills + " fill", st.stats.takes + " take · " + st.stats.quotes + " quote · " + st.stats.cancels + " cancel"),
    el("div", {class: "card kpi"}, el("div", {class: "label", text: "Pengaman risiko"}), el("div", {style: "margin-top:6px"}, guard),
      el("div", {class: "sub", text: "batas rugi harian " + usd(r.max_daily_loss) + (total ? " · " + total + " window selesai" : "")})),
  );
}

// ---------- coin cards ----------
function probTrack(m, mkt) {
  const W = 300, H = 30, pad = 6, x = (p) => pad + p * (W - 2 * pad);
  const g = sv("svg", {viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img",
    "aria-label": `P(Up) model ${p2(m && m.p_up)}, pasar ${p2(mkt)}`});
  g.append(sv("line", {x1: x(0), x2: x(1), y1: 15, y2: 15, stroke: "var(--axis)", "stroke-width": 1}));
  for (const t of [0, 0.5, 1]) g.append(sv("line", {x1: x(t), x2: x(t), y1: 11, y2: 19, stroke: "var(--axis)", "stroke-width": 1}));
  if (m && m.p_up_lo !== null && m.p_up_hi !== null)
    g.append(sv("rect", {x: x(m.p_up_lo), y: 9, width: Math.max(1, x(m.p_up_hi) - x(m.p_up_lo)), height: 12, rx: 3, fill: "var(--model-wash)"}));
  if (mkt !== null && mkt !== undefined)
    g.append(sv("rect", {x: x(mkt) - 1.5, y: 7, width: 3, height: 16, rx: 1.5, fill: "var(--market)"}));
  if (m && m.p_up !== null)
    g.append(sv("circle", {cx: x(m.p_up), cy: 15, r: 5, fill: "var(--model)", stroke: "var(--surface)", "stroke-width": 2}));
  return g;
}

function sparkline(w) {
  const pts = (w.history || []).filter(r => r[1] !== null);
  const W = 300, H = 64, padT = 4, padB = 4;
  const box = el("div", {style: "position:relative; margin-top:6px"});
  if (pts.length < 2) { box.append(el("div", {class: "muted", style: "font-size:12px; height:" + H + "px; display:grid; place-items:center", text: "grafik muncul setelah beberapa detik"})); return box; }
  const t0 = w.start, t1 = w.end, X = (t) => ((t - t0) / (t1 - t0)) * W, Y = (p) => padT + (1 - p) * (H - padT - padB);
  const g = sv("svg", {viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, preserveAspectRatio: "none", role: "img",
    "aria-label": "P(Up) model dan pasar selama window"});
  g.append(sv("line", {x1: 0, x2: W, y1: Y(0.5), y2: Y(0.5), stroke: "var(--grid)", "stroke-width": 1, "vector-effect": "non-scaling-stroke"}));
  const twapX = X(t1 - 60);
  g.append(sv("line", {x1: twapX, x2: twapX, y1: 0, y2: H, stroke: "var(--grid)", "stroke-width": 1, "vector-effect": "non-scaling-stroke"}));
  const path = (i) => pts.filter(r => r[i] !== null && r[i] !== undefined).map((r, k) => (k ? "L" : "M") + X(r[0]).toFixed(1) + "," + Y(r[i]).toFixed(1)).join("");
  g.append(sv("path", {d: path(4), fill: "none", stroke: "var(--market)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke"}));
  g.append(sv("path", {d: path(1), fill: "none", stroke: "var(--model)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke"}));
  const cross = sv("line", {x1: 0, x2: 0, y1: 0, y2: H, stroke: "var(--axis)", "stroke-width": 1, "vector-effect": "non-scaling-stroke", visibility: "hidden"});
  g.append(cross);
  const hit = sv("rect", {x: 0, y: 0, width: W, height: H, fill: "transparent", tabindex: 0, "data-wid": w.id});
  const at = (clientX) => {
    const b = g.getBoundingClientRect(), t = t0 + ((clientX - b.left) / b.width) * (t1 - t0);
    let best = pts[0]; for (const r of pts) if (Math.abs(r[0] - t) < Math.abs(best[0] - t)) best = r;
    return best;
  };
  hit._update = (clientX, clientY) => {
    const r = at(clientX); cross.setAttribute("x1", X(r[0])); cross.setAttribute("x2", X(r[0])); cross.setAttribute("visibility", "visible");
    showTip({clientX, clientY}, w.asset.toUpperCase() + " · sisa " + secs(t1 - r[0]) + " · " + clock(r[0]),
      [[lineKey("var(--model)"), "model", p2(r[1])], [lineKey("var(--market)"), "pasar", p2(r[4])]]);
  };
  hit.addEventListener("pointermove", (e) => { Object.assign(hover, {id: w.id, x: e.clientX, y: e.clientY}); hit._update(e.clientX, e.clientY); });
  hit.addEventListener("pointerleave", () => { hover.id = null; cross.setAttribute("visibility", "hidden"); hideTip(); });
  g.append(hit);
  box.append(g, el("div", {class: "row", style: "font-size:11px; color: var(--muted); gap: 12px; margin-top: 2px"},
    el("span", {}, lineKey("var(--model)"), "model"), el("span", {}, lineKey("var(--market)"), "pasar"),
    el("span", {text: "garis tipis: 50% · batas zona TWAP"})));
  return box;
}

function coinCard(w) {
  const m = w.model, mk = w.market || {};
  let st;
  if (w.phase === "upcoming") st = status("neutral", "mulai " + hm(w.start));
  else if (w.phase !== "live") st = status("neutral", w.official ? "hasil: " + w.official : "menunggu hasil");
  else if (w.tradable) st = status("good", "trading aktif");
  else st = status("warning", shortReason(w.reason), w.reason);
  const frac = Math.min(1, Math.max(0, w.elapsed / (w.end - w.start)));
  const twapFrac = Math.max(0, 1 - 60 / (w.end - w.start));
  const bar = el("div", {class: "winbar", title: "posisi waktu dalam window; garis = mulai zona TWAP 60 detik"},
    el("i", {style: "width:" + (frac * 100).toFixed(1) + "%"}), el("b", {style: "left:" + (twapFrac * 100).toFixed(1) + "%"}));
  const delta = m ? m.delta : null;
  const dcls = delta > 0 ? "up" : delta < 0 ? "down" : "";
  const gap = m && mk.p_up !== null && mk.p_up !== undefined ? m.p_up - mk.p_up : null;
  const holdings = (w.holdings || []).map(h => el("div", {}, el("span", {text: h.strategy + " · " + h.outcome}),
    el("span", {class: "num", text: h.shares.toFixed(2) + " sh · " + usd(h.cost)})));
  const orders = (w.orders || []).map(o => el("div", {class: "ink2"}, el("span", {text: "order " + o.strategy + " · " + o.outcome + " " + o.tif}),
    el("span", {class: "num", text: o.price.toFixed(3) + " × " + (o.shares - o.filled).toFixed(2)})));
  return el("div", {class: "card coin"},
    el("div", {class: "row", style: "justify-content: space-between"}, el("h3", {text: w.asset.toUpperCase()}), st),
    bar,
    el("div", {class: "winbar-labels"}, el("span", {text: hm(w.start)}), el("span", {text: w.phase === "live" ? "sisa " + secs(w.remaining) : ""}), el("span", {text: hm(w.end)})),
    el("div", {class: "line"}, el("span", {class: "ink2", text: "harga"}),
      el("span", {class: "num"}, price(m ? m.spot : null), "  ", el("b", {class: dcls, text: pct(delta, 3)}))),
    el("div", {class: "small num", title: w.ptb_source || ""}, "price to beat " + price(w.ptb) + (w.ptb_source === "official" ? " (resmi)" : "")),
    el("div", {class: "line"},
      el("span", {}, el("span", {class: "key dot"}), "model ", el("b", {class: "num", text: p2(m ? m.p_up : null)})),
      el("span", {}, el("span", {class: "key tick"}), "pasar ", el("b", {class: "num", text: p2(mk.p_up)})),
      el("span", {class: "ink2", text: "selisih " + (gap === null ? "–" : (gap > 0 ? "+" : "") + gap.toFixed(2))})),
    probTrack(m, mk.p_up),
    sparkline(w),
    el("div", {class: "small"}, m ? `vol ${Math.round(m.sigma_annual * 100)}% · z ${m.z === null ? "–" : m.z.toFixed(2)} · TWAP ${m.realized_n}/${m.samples}` : "model belum tersedia",
      " · Up ", p2(mk.up_bid), "/", p2(mk.up_ask)),
    (holdings.length || orders.length) ? el("div", {class: "pos"}, holdings, orders) : null,
  );
}

function shortReason(r) {
  if (!r) return "menunggu data";
  if (r.startsWith("no price_to_beat")) return "menunggu price to beat";
  if (r.startsWith("oracle stale")) return "oracle terlambat";
  if (r.startsWith("oracle gap")) return "data oracle bolong";
  if (r.includes("feed down")) return "feed putus";
  if (r.startsWith("waiting for order books")) return "menunggu order book";
  if (r.startsWith("model p_up")) return "model ≠ pasar";
  if (r.startsWith("insufficient history")) return "mengumpulkan data vol";
  if (r.startsWith("CEX nowcast")) return "CEX ≠ oracle";
  return r.length > 28 ? r.slice(0, 27) + "…" : r;
}

function renderCoins(st) {
  if (!st) { fill("coins", el("div", {class: "empty", text: "Belum ada window."})); return; }
  const byAsset = new Map();
  for (const w of st.windows) {
    const cur = byAsset.get(w.asset);
    const rank = (x) => x.phase === "live" ? 0 : x.phase === "upcoming" ? 1 : 2;
    if (!cur || rank(w) < rank(cur) || (rank(w) === rank(cur) && w.start < cur.start && w.phase === "upcoming")) byAsset.set(w.asset, w);
  }
  const cards = [...byAsset.values()].sort((a, b) => a.asset.localeCompare(b.asset)).map(coinCard);
  fill("coins", cards.length ? cards : el("div", {class: "empty", text: "Belum ada window yang ditemukan (cek discovery di log)."}));
  if (hover.id) {
    const hit = [...document.querySelectorAll("#coins rect[data-wid]")].find(r => r.dataset.wid === hover.id);
    if (hit && hit._update) hit._update(hover.x, hover.y); else { hover.id = null; hideTip(); }
  }
}

// ---------- P&L charts ----------
function renderPnl(summary) {
  const tl = summary ? summary.timeline : [];
  const chart = document.getElementById("pnl-chart"), table = document.getElementById("pnl-table");
  chart.hidden = state.pnlTable; table.hidden = !state.pnlTable;
  if (!tl.length) { chart.replaceChildren(el("div", {class: "empty", text: "Belum ada window yang selesai dengan posisi."})); table.replaceChildren(); return; }
  // table view
  table.replaceChildren(el("table", {}, el("thead", {}, el("tr", {}, ["Selesai", "Window", "Hasil", "P&L", "Kumulatif"].map((h, i) => el("th", {class: i > 2 ? "r" : "", text: h})))),
    el("tbody", {}, tl.slice().reverse().slice(0, 200).map(r => el("tr", {}, el("td", {text: hm(r.end)}), el("td", {text: r.window_id}), el("td", {text: r.winner || "–"}),
      el("td", {class: "r " + (r.pnl > 0 ? "up" : r.pnl < 0 ? "down" : ""), text: usd(r.pnl, true)}), el("td", {class: "r", text: usd(r.cum, true)}))))));
  // Several coins settle at the same second: one point per settlement time.
  const pts = [];
  for (const r of tl) {
    const last = pts[pts.length - 1];
    if (last && Math.abs(last.end - r.end) < 1) { last.pnl += r.pnl; last.cum = r.cum; last.n += 1; }
    else pts.push({end: r.end, pnl: r.pnl, cum: r.cum, n: 1});
  }
  const W = Math.max(300, Math.round(chart.clientWidth || 640)), H = 240, L = 56, R = 12, T = 12, B = 26;
  const ys = pts.map(r => r.cum).concat([0]);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (hi - lo < 1) { hi += 0.5; lo -= 0.5; }
  const step = niceStep((hi - lo) / 4);
  lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
  // Start the line where the running total stood when the first of these windows opened
  // ($0 unless older windows were cut off the list).
  const t0 = Math.min(tl[0].start || tl[0].end - 300, pts[0].end - 1);
  pts.unshift({end: t0, pnl: 0, cum: tl[0].cum - tl[0].pnl, n: 0, origin: true});
  const x1 = pts[pts.length - 1].end, x0 = t0;
  const X = (t) => L + (t - x0) / (x1 - x0) * (W - L - R), Y = (v) => T + (hi - v) / (hi - lo) * (H - T - B);
  const g = sv("svg", {viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": "P&L kumulatif per window yang selesai"});
  for (let v = lo; v <= hi + 1e-9; v += step) {
    g.append(sv("line", {x1: L, x2: W - R, y1: Y(v), y2: Y(v), stroke: Math.abs(v) < 1e-9 ? "var(--axis)" : "var(--grid)", "stroke-width": 1}));
    g.append(sv("text", {x: L - 8, y: Y(v) + 4, "text-anchor": "end"}, usd(v)));
  }
  g.append(sv("text", {x: L, y: H - 6}, hm(x0)), sv("text", {x: W - R, y: H - 6, "text-anchor": "end"}, hm(x1)));
  const d = pts.map((r, i) => (i ? "L" : "M") + X(r.end).toFixed(1) + "," + Y(r.cum).toFixed(1)).join("");
  g.append(sv("path", {d: d + `L${X(x1).toFixed(1)},${Y(0).toFixed(1)}L${X(x0).toFixed(1)},${Y(0).toFixed(1)}Z`, fill: "var(--model-wash)"}));
  g.append(sv("path", {d, fill: "none", stroke: "var(--model)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"}));
  const last = pts[pts.length - 1];
  g.append(sv("circle", {cx: X(last.end), cy: Y(last.cum), r: 4, fill: "var(--model)", stroke: "var(--surface)", "stroke-width": 2}));
  const cross = sv("line", {x1: 0, x2: 0, y1: T, y2: H - B, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden"});
  const dot = sv("circle", {r: 4, fill: "var(--model)", stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden"});
  g.append(cross, dot);
  const hit = sv("rect", {x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent", tabindex: 0});
  hit.addEventListener("pointermove", (e) => {
    const b = g.getBoundingClientRect(), t = x0 + ((e.clientX - b.left) / b.width * W - L) / (W - L - R) * (x1 - x0);
    let best = pts[1]; for (const r of pts) if (!r.origin && Math.abs(r.end - t) < Math.abs(best.end - t)) best = r;
    cross.setAttribute("x1", X(best.end)); cross.setAttribute("x2", X(best.end)); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", X(best.end)); dot.setAttribute("cy", Y(best.cum)); dot.setAttribute("visibility", "visible");
    showTip(e, "selesai " + hm(best.end) + " · " + best.n + " window", [["", "P&L slot ini", usd(best.pnl, true)], [lineKey("var(--model)"), "kumulatif", usd(best.cum, true)]]);
  });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(); });
  g.append(hit);
  const nearLeft = X(last.end) < L + 90;
  g.append(sv("text", {x: X(last.end) + (nearLeft ? 8 : -6), y: Y(last.cum) - 8, "text-anchor": nearLeft ? "start" : "end",
    style: "fill: var(--ink); font-weight: 600"}, usd(last.cum, true)));
  chart.replaceChildren(g);
}
function niceStep(x) { const p = Math.pow(10, Math.floor(Math.log10(Math.max(x, 1e-9)))); const f = x / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p; }

function renderStrategies(summary) {
  const rows = summary ? Object.entries(summary.by_strategy).sort((a, b) => b[1].pnl - a[1].pnl) : [];
  if (!rows.length) { fill("strategies", el("div", {class: "empty", text: "Belum ada hasil per strategi."})); return; }
  const maxAbs = Math.max(...rows.map(([, s]) => Math.abs(s.pnl)), 0.01);
  const anyNeg = rows.some(([, s]) => s.pnl < 0), anyPos = rows.some(([, s]) => s.pnl > 0);
  const zero = anyNeg && anyPos ? 0.5 : anyNeg ? 0.92 : 0.0;  // where the baseline sits (fraction of width)
  const span = anyNeg && anyPos ? 0.38 : 0.62;
  fill("strategies", rows.map(([name, s]) => {
    const w = Math.abs(s.pnl) / maxAbs * span * 100, pos = s.pnl >= 0;
    const bar = el("div", {class: "bar " + (pos ? "pos" : "neg"), style: `left:${pos ? zero * 100 : zero * 100 - w}%; width:${Math.max(w, 0.6)}%`});
    const val = el("span", {class: "bar-val", text: usd(s.pnl, true), style: pos ? `left:calc(${zero * 100 + w}% + 6px)` : `right:calc(${100 - zero * 100 + w}% + 6px)`});
    const track = el("div", {class: "bar-track", tabindex: 0}, el("span", {class: "bar-zero", style: `left:${zero * 100}%`}), bar, val);
    track.addEventListener("pointermove", (e) => showTip(e, name, [["", "P&L", usd(s.pnl, true)], ["", "window", String(s.windows)],
      ["", "menang", Math.round(s.wins / s.windows * 100) + "%"], ["", "terburuk", usd(s.worst, true)]]));
    track.addEventListener("pointerleave", hideTip);
    return el("div", {class: "bar-row"}, el("div", {class: "bar-name"}, name, el("small", {text: s.windows + " window · menang " + Math.round(s.wins / s.windows * 100) + "%"})), track);
  }));
}

// ---------- feeds / calibration / trades ----------
function renderFeeds(st) {
  if (!st) { fill("feeds", el("div", {class: "empty", text: "–"})); return; }
  const feeds = (st.feeds || []).slice().sort((a, b) => a.name.localeCompare(b.name));
  if (!feeds.length) { fill("feeds", el("div", {class: "empty", text: "Belum ada feed."})); return; }
  fill("feeds", el("table", {}, el("thead", {}, el("tr", {}, ["Feed", "Status", "Data terakhir", "Cara subscribe", "Reconnect"].map((h, i) => el("th", {class: i >= 2 && i !== 3 ? "r" : "", text: h})))),
    el("tbody", {}, feeds.map(f => {
      let s;
      if (!f.connected) s = status("critical", "putus");
      else if (f.data_age !== null && f.data_age > 10) s = status("warning", "sepi");
      else s = status("good", "ok");
      const style = f.style ? f.style + (f.streaming ? " ✓" : "") : "–";
      return el("tr", {}, el("td", {text: f.name}), el("td", {}, s),
        el("td", {class: "r", text: f.data_age === null ? "–" : f.data_age.toFixed(1) + " dtk"}),
        el("td", {class: "ink2", text: style}), el("td", {class: "r", text: String(f.reconnects)}));
    }))));
}

function renderCalibration(summary) {
  if (!summary) return;
  const cal = summary.calibration, rules = summary.rules;
  const parts = [];
  if (!cal || !cal.n) parts.push(el("div", {class: "empty", text: "Belum ada window dengan hasil untuk dinilai."}));
  else {
    parts.push(el("p", {class: "muted", style: "margin: 0 0 8px; font-size: 12px", text: "Brier score: makin kecil makin akurat. Model harus mengalahkan pasar agar punya edge."}));
    parts.push(el("table", {}, el("thead", {}, el("tr", {}, ["Sisa waktu", "n", "Model", "Pasar", "Lebih akurat"].map((h, i) => el("th", {class: i > 0 && i < 4 ? "r" : "", text: h})))),
      el("tbody", {}, cal.buckets.map(b => el("tr", {}, el("td", {text: b.label}), el("td", {class: "r", text: String(b.n)}),
        el("td", {class: "r", text: b.model.toFixed(4)}), el("td", {class: "r", text: b.market.toFixed(4)}),
        el("td", {}, b.verdict === "model" ? status("good", "model") : b.verdict === "market" ? status("warning", "pasar") : status("neutral", "seri")))))));
  }
  if (rules && rules.windows) {
    const line = (name, r) => r.n ? el("div", {class: "line", style: "display:flex; justify-content:space-between; font-size:13px; margin-top:6px"},
      el("span", {text: name}), el("span", {class: "num", text: `${r.ok}/${r.n} (${(r.ok / r.n * 100).toFixed(1)}%)`})) : null;
    parts.push(el("h2", {style: "margin-top: 16px", text: "Aturan settlement vs hasil resmi"}),
      line("aturan TWAP", rules.twap), line("aturan harga terakhir", rules.last));
  }
  fill("calibration", parts);
}

function renderTrades(summary) {
  const rows = summary ? summary.trades.slice().reverse() : [];
  if (!rows.length) { fill("trades", el("div", {class: "empty", text: "Belum ada transaksi."})); return; }
  const sm = [2, 6, 8];  // columns hidden on phones
  fill("trades", el("table", {}, el("thead", {}, el("tr", {}, ["Waktu", "Strategi", "Window", "Sisi", "Outcome", "Harga", "Shares", "$", "Alasan"].map((h, i) =>
    el("th", {class: (i >= 5 && i <= 7 ? "r " : "") + (sm.includes(i) ? "hide-sm" : ""), text: h})))),
    el("tbody", {}, rows.map(r => {
      const side = r.side === "BUY" ? status("neutral", "BUY") : r.side === "SETTLE" ? (Number(r.price) >= 1 ? status("good", "menang") : status("critical", "kalah")) : status("warning", r.side);
      const t = r.timestamp ? new Date(r.timestamp).toLocaleTimeString("id-ID") : "";
      return el("tr", {}, el("td", {class: "num", text: t}), el("td", {text: r.strategy}), el("td", {class: "ink2 hide-sm", text: r.market_id}), el("td", {}, side),
        el("td", {text: r.outcome}), el("td", {class: "r", text: Number(r.price).toFixed(3)}), el("td", {class: "r hide-sm", text: Number(r.size_shares).toFixed(2)}),
        el("td", {class: "r", text: usd(Number(r.size_usd))}), el("td", {class: "reason hide-sm", title: r.reason, text: r.reason}));
    }))));
}

// ---------- loop ----------
async function getJson(url) { const r = await fetch(url, {cache: "no-store"}); if (!r.ok) throw new Error(r.status); return r.json(); }
async function tickLive() {
  try { state.live = await getJson("/api/live"); }
  catch (e) { state.live = null; }
  const st = state.live && state.live.status;
  renderHeader(state.live); renderKpis(st, state.summary); renderCoins(st); renderFeeds(st);
}
async function tickSummary() {
  try { state.summary = await getJson("/api/summary"); } catch (e) { return; }
  renderPnl(state.summary); renderStrategies(state.summary); renderCalibration(state.summary); renderTrades(state.summary);
}
document.getElementById("coins").addEventListener("pointerleave", () => { hover.id = null; hideTip(); });
document.getElementById("pnl-toggle").addEventListener("click", (e) => {
  state.pnlTable = !state.pnlTable; e.target.textContent = state.pnlTable ? "grafik" : "tabel"; renderPnl(state.summary);
});
document.getElementById("theme").addEventListener("click", () => {
  const root = document.documentElement, dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const cur = root.dataset.theme || (dark ? "dark" : "light"), next = cur === "dark" ? "light" : "dark";
  root.dataset.theme = next; try { localStorage.setItem("updown-theme", next); } catch (e) {}
});
try { const t = localStorage.getItem("updown-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
let resizeTimer = null;
addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => renderPnl(state.summary), 150); });
tickLive(); tickSummary();
setInterval(tickLive, 1000); setInterval(tickSummary, 15000);
</script>
</body>
</html>
"""


def make_handler(source: DataSource):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/live":
                    self._send(200, json.dumps(source.live()).encode("utf-8"), "application/json")
                elif path == "/api/summary":
                    self._send(200, json.dumps(source.summary()).encode("utf-8"), "application/json")
                elif path in ("/", "/index.html"):
                    self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                else:
                    self._send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.path.join(_ROOT, "data"))
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = reachable from other devices on your network (no login!)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("UPDOWN_DASHBOARD_PORT", "8766")))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(DataSource(args.data_dir)))
    url = f"http://127.0.0.1:{args.port}"
    print(f"Dashboard Up/Down jalan di {url}  (Ctrl+C untuk berhenti)")
    if args.host not in ("127.0.0.1", "localhost"):
        print("PERINGATAN: bisa dibuka dari perangkat lain di jaringanmu tanpa login (hanya-baca).")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard dihentikan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
