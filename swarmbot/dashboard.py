"""Read-only dashboard: python -m swarmbot dashboard (http://127.0.0.1:8790).

Reads data/swarmbot/status.json (written by the bot every ~10 seconds) and
trades.csv. It cannot buy or sell anything; stopping it never affects the bot.
Self-contained page: no CDN.
"""
from __future__ import annotations

import csv
import json
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STALE_S = 60


def load_status(data_dir: Path) -> dict:
    try:
        status = json.loads((data_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"missing": True}
    status["age_s"] = time.time() - float(status.get("updated_at") or 0)
    status["stale"] = status["age_s"] > STALE_S
    return status


def load_trades(data_dir: Path) -> dict:
    path = data_dir / "trades.csv"
    rows: list[dict] = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    sells = [r for r in rows if r.get("action") == "SELL"]
    by_mood: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl_usd": 0.0})
    curve, total = [], 0.0
    for r in sells:
        pnl = float(r.get("pnl_usd") or 0)
        total += pnl
        curve.append({"time": r["time"], "pnl": round(total, 4)})
        m = by_mood[r.get("mood") or "?"]
        m["trades"] += 1
        m["wins"] += pnl > 0
        m["pnl_usd"] += pnl
    wins = sum(1 for r in sells if float(r.get("pnl_usd") or 0) > 0)
    return {
        "closed": len(sells), "wins": wins, "pnl_usd": total,
        "tp": sum(1 for r in sells if r.get("reason") == "TP"),
        "sl": sum(1 for r in sells if r.get("reason") == "SL"),
        "by_mood": by_mood, "curve": curve, "recent": rows[-50:][::-1],
    }


class Handler(BaseHTTPRequestHandler):
    data_dir = Path("data/swarmbot")

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/data":
            data = {"status": load_status(self.data_dir), "trades": load_trades(self.data_dir)}
            self._send(200, json.dumps(data, default=str).encode("utf-8"), "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def make_server(data_dir: Path, host: str = "127.0.0.1", port: int = 8790) -> ThreadingHTTPServer:
    handler = type("SwarmHandler", (Handler,), {"data_dir": Path(data_dir)})
    return ThreadingHTTPServer((host, port), handler)


def serve(data_dir: Path, host: str = "127.0.0.1", port: int = 8790) -> int:
    try:
        server = make_server(data_dir, host, port)
    except OSError as exc:
        print(f"GAGAL: port {port} tidak bisa dipakai ({exc}). Coba --port {port + 1}.")
        return 1
    print(f"Dashboard swarmbot: http://{host}:{port}  (Ctrl+C = berhenti; bot tidak terpengaruh)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>swarmbot</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262a33;--text:#e6e8ec;--dim:#8b92a0;--up:#2fbf71;--down:#ef5350;--warn:#e0a526;--acc:#6aa7ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;justify-content:space-between;margin-bottom:12px}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:0 0 8px;color:var(--dim);font-weight:600}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:#222733;color:var(--dim)}
.ok{color:var(--up)}.bad{color:var(--down)}.warn{color:var(--warn)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px;overflow-x:auto}
.grid .card{margin:0}.k{color:var(--dim);font-size:12px}.v{font-size:22px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child,th.l,td.l{text-align:left}th{color:var(--dim);font-weight:500;font-size:12px}
a{color:var(--acc);text-decoration:none}
.mood{padding:1px 7px;border-radius:6px;font-size:12px;background:#222733}
.m-shocked{background:#3d2a12;color:#ffb74d}.m-happy{background:#123d24;color:#69f0ae}.m-calm{background:#12303d;color:#81d4fa}
.bars{display:flex;flex-wrap:wrap;gap:6px}.bars span{font-size:12px}
svg{width:100%;height:120px;display:block}
.empty{color:var(--dim);padding:8px 0}
</style></head><body><main>
<header><h1>swarmbot <span class="pill">PAPER</span></h1><div id="hb" class="pill">memuat…</div></header>
<div class="grid">
 <div class="card"><div class="k">Nilai total</div><div class="v" id="eq">–</div><div class="k" id="eqd"></div></div>
 <div class="card"><div class="k">Kas</div><div class="v" id="cash">–</div></div>
 <div class="card"><div class="k">Hasil ditutup</div><div class="v" id="pnl">–</div><div class="k" id="tpsl"></div></div>
 <div class="card"><div class="k">Menang</div><div class="v" id="wr">–</div><div class="k" id="cl"></div></div>
 <div class="card"><div class="k">Posisi terbuka</div><div class="v" id="np">–</div><div class="k" id="rules"></div></div>
</div>
<div class="card"><h2>Hasil kumulatif (USD)</h2><div id="curve" class="empty">Belum ada posisi yang ditutup.</div></div>
<div class="card"><h2>Posisi terbuka</h2><div id="pos"></div></div>
<div class="card"><h2>Hasil per mood</h2><div id="moods"></div></div>
<div class="card"><h2>Mood semua token (scan terakhir)</h2><div id="counts" class="bars"></div></div>
<div class="card"><h2>Kandidat shocked / happy / calm</h2><div id="cand"></div></div>
<div class="card"><h2>Transaksi terakhir</h2><div id="trades"></div></div>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const usd=(x,d=2)=>(x<0?"-$":"$")+Math.abs(+x||0).toFixed(d);
const sgn=x=>`<span class="${x>0?"ok":x<0?"bad":""}">${x>0?"+":""}${(+x).toFixed(1)}%</span>`;
const sgnu=x=>`<span class="${x>0?"ok":x<0?"bad":""}">${x>0?"+":""}${usd(x)}</span>`;
const px=p=>p>=1?(+p).toFixed(4):(+p).toPrecision(4);
const mood=m=>`<span class="mood m-${esc(m)}">${esc(m)}</span>`;
const tok=(s,m)=>`<a href="https://gmgn.ai/sol/token/${encodeURIComponent(m)}" target="_blank" rel="noopener">${esc(s)}</a>`;
const ago=s=>s<90?Math.round(s)+" dtk":s<5400?Math.round(s/60)+" mnt":(s/3600).toFixed(1)+" jam";
function table(head,rows,empty){if(!rows.length)return`<div class="empty">${empty}</div>`;
 return`<table><tr>${head.map((h,i)=>`<th class="${i<2?"l":""}">${h}</th>`).join("")}</tr>${rows.map(r=>`<tr>${r.map((c,i)=>`<td class="${i<2?"l":""}">${c}</td>`).join("")}</tr>`).join("")}</table>`}
function curve(pts){if(!pts.length)return'<div class="empty">Belum ada posisi yang ditutup.</div>';
 const v=[0,...pts.map(p=>p.pnl)],W=1000,H=120,mn=Math.min(0,...v),mx=Math.max(0,...v),r=(mx-mn)||1;
 const xy=v.map((y,i)=>[i/(v.length-1||1)*W,H-6-(y-mn)/r*(H-12)]);
 const z=H-6-(0-mn)/r*(H-12),last=v[v.length-1],col=last>=0?"var(--up)":"var(--down)";
 return`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line x1="0" x2="${W}" y1="${z}" y2="${z}" stroke="#333a46" stroke-dasharray="4 4"/><polyline fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke" points="${xy.map(p=>p.join(",")).join(" ")}"/></svg><div class="k">${pts.length} posisi ditutup · total ${sgnu(last)}</div>`}
async function tick(){
 let d;try{d=await (await fetch("/api/data",{cache:"no-store"})).json()}catch(e){$("hb").innerHTML='<span class="bad">dashboard tidak terhubung</span>';return}
 const s=d.status,t=d.trades;
 if(s.missing){$("hb").innerHTML='<span class="warn">bot belum menulis data (baru start?)</span>'}
 else{$("hb").innerHTML=s.stale?`<span class="bad">bot tidak update ${ago(s.age_s)}; cek: systemctl status swarmbot</span>`
   :`<span class="ok">● bot jalan</span> · update ${ago(s.age_s)} lalu${s.last_scan_ok?"":' · <span class="warn">scan terakhir gagal</span>'}${s.buy_block?` · <span class="warn">tidak beli: ${esc(s.buy_block)}</span>`:""}`;
  const st=s.settings||{},start=st.starting_cash_usd||0;
  $("eq").textContent=usd(s.equity);$("eqd").innerHTML=start?`awal ${usd(start)} · ${sgn((s.equity/start-1)*100)}`:"";
  $("cash").textContent=usd(s.cash);$("np").textContent=`${s.positions.length} / ${st.max_open_positions??"?"}`;
  $("rules").textContent=`TP +${st.take_profit_pct}% · SL -${st.stop_loss_pct}% · ${usd(st.position_usd,0)}/posisi`;
  $("pos").innerHTML=table(["Token","Mood","Harga beli","Harga kini","Gerak","Nilai","Harga dicek","Sejak"],
   s.positions.sort((a,b)=>b.move_pct-a.move_pct).map(p=>[tok(p.symbol,p.mint),mood(p.mood),px(p.entry_price),px(p.price),sgn(p.move_pct),usd(p.value_usd),p.price_age_s==null?"–":(p.price_age_s>30?`<span class="warn">${ago(p.price_age_s)} lalu</span>`:ago(p.price_age_s)+" lalu"),ago(Date.now()/1000-p.opened_at)]),"Tidak ada posisi terbuka.");
  const c=Object.entries(s.mood_counts||{}).sort((a,b)=>b[1]-a[1]);
  $("counts").innerHTML=c.length?c.map(([m,n])=>`<span>${mood(m)} ${n}</span>`).join(""):'<span class="empty">–</span>';
  $("cand").innerHTML=table(["Token","Mood","5m","1j","Likuiditas","Mcap","Status"],
   (s.candidates||[]).map(x=>[tok(x.symbol,x.mint),mood(x.mood),sgn(x.change_5m),sgn(x.change_1h),usd(x.liquidity,0),usd(x.mcap,0),x.skip?`<span class="k">${esc(x.skip)}</span>`:(s.buy_block?`<span class="warn">menunggu: ${esc(s.buy_block)}</span>`:'<span class="ok">boleh dibeli</span>')]),"Tidak ada token dengan mood ini sekarang.");}
 $("pnl").innerHTML=sgnu(t.pnl_usd);$("tpsl").textContent=`TP ${t.tp} · SL ${t.sl}`;
 $("wr").textContent=t.closed?Math.round(t.wins/t.closed*100)+"%":"–";$("cl").textContent=`${t.wins} dari ${t.closed} posisi`;
 $("curve").innerHTML=curve(t.curve);
 $("moods").innerHTML=table(["Mood","","Posisi","Menang","Hasil"],Object.entries(t.by_mood).map(([m,v])=>[mood(m),"",v.trades,Math.round(v.wins/v.trades*100)+"%",sgnu(v.pnl_usd)]),"Belum ada.");
 $("trades").innerHTML=table(["Waktu (UTC)","Token","Aksi","Mood","Harga","USD","Hasil","Alasan"],
  t.recent.map(r=>[esc(r.time.replace("T"," ").replace("Z","")),tok(r.symbol,r.mint),r.action==="BUY"?'<span class="ok">BELI</span>':'<span class="bad">JUAL</span>',mood(r.mood),px(+r.price),usd(r.usd),r.pnl_usd?sgnu(+r.pnl_usd)+" ("+sgn(+r.pnl_pct)+")":"",esc(r.reason)]),"Belum ada transaksi.");
}
tick();setInterval(tick,2000);
</script></body></html>
"""
