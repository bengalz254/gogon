"""Command line: python -m migbot {run,check,dashboard,report,reset}."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

from migbot.config import ConfigError, load_settings


def _settings(args):
    try:
        return load_settings(args.config)
    except ConfigError as exc:
        print(f"GAGAL: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_run(args) -> int:
    from migbot.engine import Engine, Sources
    from migbot.logger import setup_logging
    from migbot.notifier import Notifier

    s = _settings(args)
    setup_logging(s.log_dir)
    notifier = Notifier(s.telegram_token, s.telegram_chat_id, enabled=s.notify.telegram_enabled)
    Engine(s, Sources.build(s), notifier).run()
    return 0


def cmd_check(args) -> int:
    from migbot.check import run_check

    return run_check(_settings(args), telegram_test=args.telegram_test)


def cmd_dashboard(args) -> int:
    from migbot.dashboard import serve

    s = _settings(args)
    return serve(s.data_dir, host=args.host, port=args.port, open_browser=not args.no_browser)


def cmd_report(args) -> int:
    from migbot.report import format_report, summarize

    s = _settings(args)
    print(format_report(summarize(s.data_dir)))
    return 0


def cmd_reset(args) -> int:
    from migbot.storage import read_json

    s = _settings(args)
    status = read_json(os.path.join(s.data_dir, "status.json")) or {}
    if status.get("running") and time.time() - float(status.get("updated_at") or 0) < 60:
        print("GAGAL: bot masih jalan. Hentikan dulu (sudo systemctl stop migbot), lalu ulangi.", file=sys.stderr)
        return 1
    names = ["state.json", "status.json", "tokens.csv", "trades.csv"]
    present = [n for n in names if os.path.exists(os.path.join(s.data_dir, n))]
    if not present:
        print("Tidak ada data untuk direset.")
        return 0
    if not args.yes:
        answer = input(f"Pindahkan {', '.join(present)} ke arsip dan mulai dari nol? [y/N] ")
        if answer.strip().lower() not in ("y", "ya", "yes"):
            print("Dibatalkan.")
            return 1
    archive = os.path.join(s.data_dir, "archive", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(archive, exist_ok=True)
    for name in present:
        shutil.move(os.path.join(s.data_dir, name), os.path.join(archive, name))
    print(f"Data lama dipindah ke {archive}. Saldo paper kembali ke {s.trading.paper_balance_sol:g} SOL saat bot start.")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # a Windows console or redirect in a legacy code page must not crash on ✅ or ≥
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="python -m migbot", description="Bot meme coin migrated (paper trading)")
    parser.add_argument("--config", default=None, help="file konfigurasi (default config/migbot.yaml)")
    # --config is also accepted after the command (python -m migbot run --config x.yaml).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", parents=[common], help="jalankan bot").set_defaults(func=cmd_run)
    p = sub.add_parser("check", parents=[common], help="cek konfigurasi dan koneksi ke semua sumber data")
    p.add_argument("--telegram-test", action="store_true", help="kirim pesan tes ke Telegram")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("dashboard", parents=[common], help="dashboard lokal (hanya baca)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8780)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_dashboard)
    sub.add_parser("report", parents=[common], help="laporan riset dan P&L").set_defaults(func=cmd_report)
    p = sub.add_parser("reset", parents=[common], help="arsipkan data dan mulai dari nol")
    p.add_argument("--yes", action="store_true", help="tanpa konfirmasi")
    p.set_defaults(func=cmd_reset)
    args = parser.parse_args(argv)
    return args.func(args)
