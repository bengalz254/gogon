"""python -m swarmbot run | check | moods | report | dashboard"""
from __future__ import annotations

import argparse
import csv
import logging
import signal
import sys
from collections import defaultdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

from swarmbot import config as config_mod
from swarmbot.engine import Engine
from swarmbot.jupiter import Jupiter, JupiterError
from swarmbot.paper import Paper


def _logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout),
              RotatingFileHandler("logs/swarmbot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_run(cfg) -> int:
    engine = Engine(cfg)

    def stop(*_):
        engine.stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    engine.run()
    return 0


def cmd_moods(cfg, show: int = 15) -> int:
    engine = Engine(cfg, paper=Paper(cfg.data_dir / "_preview", cfg.starting_cash_usd, cfg.fee_pct, cfg.currency))
    try:
        labelled = engine.scan()
    except JupiterError as exc:
        print(f"[GAGAL] {exc}")
        return 1
    print(f"\nToken dengan mood yang dibeli bot ({', '.join(cfg.buy_moods)}):")
    rows = [(t, m) for t, m in labelled if m in cfg.buy_moods]
    rows.sort(key=lambda tm: (cfg.buy_moods.index(tm[1]), -tm[0].liquidity))
    for t, m in rows[:show]:
        why = engine.eligible(t, m) or "BOLEH DIBELI"
        print(f"  {m:8} {t.symbol[:12]:12} 5m {t.change_5m:+7.1f}%  1h {t.change_1h:+7.1f}%  "
              f"liq ${t.liquidity:>12,.0f}  -> {why}")
    if not rows:
        print("  (tidak ada saat ini)")
    return 0


def cmd_check(cfg) -> int:
    print(f"Jupiter: {cfg.jupiter_base_url} ({'dengan' if cfg.jupiter_api_key else 'tanpa'} API key)")
    jup = Jupiter(cfg.jupiter_base_url, cfg.jupiter_api_key)
    ok = 0
    for name in cfg.jupiter_lists:
        try:
            n = len(jup.token_list(name, cfg.jupiter_limit))
            print(f"  [OK] {name}: {n} token")
            ok += 1
        except JupiterError as exc:
            print(f"  [GAGAL] {name}: {exc}")
    if not ok:
        print("\nSemua daftar gagal. Cek internet VPS, atau isi JUPITER_API_KEY di .env (gratis di portal.jup.ag).")
        return 1
    return cmd_moods(cfg, show=10)


def cmd_report(cfg) -> int:
    paper = Paper(cfg.data_dir, cfg.starting_cash_usd, cfg.fee_pct, cfg.currency)
    sells = []
    if paper.journal_path.exists():
        with paper.journal_path.open(encoding="utf-8") as f:
            sells = [r for r in csv.DictReader(f) if r["action"] == "SELL"]
    cur = cfg.currency
    print(f"Saldo awal       : {cfg.starting_cash_usd:.4f} {cur}")
    print(f"Saldo kas        : {paper.cash:.4f} {cur}")
    print(f"Nilai total kira2: {paper.equity():.4f} {cur}")
    print(f"Posisi terbuka   : {len(paper.positions)}")
    for p in paper.positions.values():
        move = (p.last_price / p.entry_price - 1) * 100 if p.last_price else 0.0
        print(f"   {p.symbol:12} {p.mood:8} beli {p.entry_price:.10g}  sekarang {move:+.1f}%")
    if not sells:
        print("Belum ada posisi yang ditutup.")
        return 0
    total = sum(float(r["pnl_usd"]) for r in sells)
    wins = sum(1 for r in sells if float(r["pnl_usd"]) > 0)
    print(f"Posisi ditutup   : {len(sells)} (untung {wins}, rugi {len(sells) - wins}, "
          f"menang {wins / len(sells) * 100:.0f}%)")
    print(f"Total hasil      : {total:+.4f} {cur}")
    by = defaultdict(list)
    for r in sells:
        by[r["mood"]].append(float(r["pnl_usd"]))
    print("Per mood:")
    for mood, pnls in sorted(by.items()):
        w = sum(1 for x in pnls if x > 0)
        print(f"   {mood:8} {len(pnls):4} trade  menang {w / len(pnls) * 100:3.0f}%  hasil {sum(pnls):+.4f} {cur}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="swarmbot", description="Bot paper jual beli mood dotswarm.fun")
    ap.add_argument("command", choices=["run", "check", "moods", "report", "dashboard"])
    ap.add_argument("--config", default=str(config_mod.DEFAULT_PATH))
    ap.add_argument("--port", type=int, default=8790, help="port dashboard (default 8790)")
    ap.add_argument("--host", default="127.0.0.1", help="alamat dashboard (default hanya lokal)")
    args = ap.parse_args(argv)
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        print(f"Konfigurasi salah: {exc}", file=sys.stderr)
        return 2
    if args.command == "dashboard":
        from swarmbot.dashboard import serve
        return serve(cfg.data_dir, args.host, args.port)
    if args.command == "run":
        _logging()
        return cmd_run(cfg)
    logging.basicConfig(level=logging.WARNING)
    return {"check": cmd_check, "moods": cmd_moods, "report": cmd_report}[args.command](cfg)
