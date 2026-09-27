"""How the paper grid is doing: its status and latest trades.

    python scripts/spot_status.py              # summary + the last 10 trades
    python scripts/spot_status.py --trades 30

Reads data/spot/status.json and data/spot/trades.csv, which the bot keeps up to date.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from spot.config import DATA_DIR  # noqa: E402
from spot.grid import fmt_amount, fmt_money  # noqa: E402
from spot.records import TradeLog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the paper grid's status and latest trades")
    parser.add_argument("--trades", type=int, default=10, help="how many recent trades to show")
    parser.add_argument("--data-dir", default=os.path.join(_ROOT, DATA_DIR), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        with open(os.path.join(args.data_dir, "status.json"), encoding="utf-8") as f:
            status = json.load(f)
    except (OSError, ValueError):
        print("Belum ada status: bot grid belum pernah jalan (python -m spot.main).")
        return 1

    symbol = status.get("symbol") or "?/?"
    base, quote = (symbol.split("/") + ["?"])[:2]
    money = lambda value: fmt_money(value, quote) if value is not None else "-"  # noqa: E731
    age = time.time() - float(status.get("updated_ts", 0))
    state = "jalan" if status.get("running") else "berhenti"
    print(f"Bot grid {symbol} (PAPER) — {state}, diperbarui {age:.0f} detik lalu")
    if status.get("error"):
        print(f"⛔ Tidak bisa mulai: {status['error']}")
        return 1
    if status.get("last_error"):
        print(f"⚠️ Error terakhir: {status['last_error']}")
    if "grid" not in status:
        return 0

    g = status["grid"]
    start = status["start_balance"]
    total = status["total_pnl"]
    print(f"Harga {money(status.get('price'))} | range {money(g['lower'])} – {money(g['upper'])}, {g['slots']} slot")
    print(f"Order menunggu: {g['buy_orders']} beli, {g['sell_orders']} jual")
    print(f"Memegang {fmt_amount(status['holding'])} {base} | uang {money(status['quote_balance'])}")
    print(f"Putaran selesai: {status['round_trips']} | untung terealisasi {money(status['realized_pnl'])}")
    print(f"Belum terealisasi: {money(status['unrealized_pnl'])}")
    print(f"Nilai akun {money(status['equity'])} ({money(total)}, {total / start * 100:+.2f}% dari modal {money(start)})")
    print(f"Biaya {money(status['fees'])} + pajak {money(status['taxes'])} sejak {status.get('started_at')}")
    if status.get("hodl_pnl") is not None:
        print(f"Pembanding — kalau modal dibelikan {base} semua sejak awal: {money(status['hodl_pnl'])}")
    if status.get("stopped"):
        print(f"⛔ Grid berhenti: {status.get('stop_reason')}")

    trades = TradeLog(os.path.join(args.data_dir, "trades.csv")).tail(args.trades)
    if trades:
        print(f"\n{len(trades)} transaksi terakhir:")
        for t in trades:
            pnl = f"  untung {money(float(t['pnl']))}" if t.get("pnl") else ""
            note = f"  ({t['note']})" if t.get("note") else ""
            side = "BELI" if t["side"] == "BUY" else "JUAL"
            print(f"  {t['timestamp']}  {side:<4} {fmt_amount(float(t['amount']))} {base} @ {money(float(t['price']))}{pnl}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
