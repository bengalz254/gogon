"""Check the Tokocrypto grid bot's setup before running it.

    python scripts/spot_check.py                   # connection, pair, grid preview
    python scripts/spot_check.py --symbols IDR     # list the pairs quoted in IDR
    python scripts/spot_check.py --telegram-test   # also send a Telegram test message

Nothing here places an order or needs an API key. Exit code 0 = ready.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests  # noqa: E402

from bot.notify import NotificationError, Notifier  # noqa: E402
from spot.bot import MODE  # noqa: E402
from spot.config import ConfigError, load_settings  # noqa: E402
from spot.grid import auto_range, fmt_amount, fmt_money, plan_grid  # noqa: E402
from spot.market import CANDLE_MS, DataError, SymbolError, TokocryptoData  # noqa: E402
from spot.records import JsonStore  # noqa: E402

HOSTS = {
    "tokocrypto": ("Tokocrypto", "https://www.tokocrypto.com/open/v1/common/time"),
    "binance": ("Binance (data untuk sebagian pasangan)", "https://api.binance.com/api/v3/ping"),
}


def check_host(name: str, url: str) -> bool:
    try:
        resp = requests.get(url, timeout=10)
    except requests.RequestException as exc:
        print(f"  GAGAL  {name}: {type(exc).__name__} (internet/DNS/firewall?)")
        return False
    if resp.status_code == 200:
        print(f"  OK     {name}")
        return True
    hint = " — lokasi server (VPS) diblokir" if resp.status_code in (403, 451) else ""
    print(f"  GAGAL  {name}: HTTP {resp.status_code}{hint}")
    return False


def list_symbols(quote: str | None) -> int:
    try:
        pairs = TokocryptoData("BTC/IDR").symbols(quote.upper() if quote else None)
    except DataError as exc:
        print(exc)
        return 1
    for symbol, native in pairs:
        print(f"{symbol:<16} data dari {'Tokocrypto' if native else 'Binance'}")
    print(f"\n{len(pairs)} pasangan" + (f" dengan {quote.upper()}" if quote else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Tokocrypto grid bot's setup")
    parser.add_argument("--config", help="path to the settings file (default: config/spot.yaml)")
    parser.add_argument("--symbols", nargs="?", const="", metavar="QUOTE", help="list trading pairs (e.g. IDR)")
    parser.add_argument("--telegram-test", action="store_true", help="send a Telegram test message")
    args = parser.parse_args(argv)

    if args.symbols is not None:
        return list_symbols(args.symbols or None)

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"Config salah: {exc}")
        return 1
    money = lambda value: fmt_money(value, settings.quote)  # noqa: E731
    ok = True

    print("== 1. Koneksi ==")
    reachable = {key: check_host(name, url) for key, (name, url) in HOSTS.items()}

    print(f"\n== 2. Pasangan {settings.symbol} ==")
    data = TokocryptoData(settings.symbol)
    try:
        rules = data.rules()
        bid, ask = data.top_of_book()
        now = time.time()
        candles = data.closed_candles(int(now * 1000) - 10 * CANDLE_MS, int(now * 1000))
    except (DataError, SymbolError) as exc:
        print(f"  {exc}")
        return 1
    source = "Tokocrypto" if rules.native else "Binance (api.binance.com)"
    print(f"  Data harga dari: {source}")
    if not rules.native and not reachable["binance"]:
        print("  MASALAH: data pasangan ini lewat Binance, dan Binance tidak bisa diakses dari server ini.")
        ok = False
    minimum = [f"{fmt_amount(rules.min_amount)} {rules.base}"] if rules.min_amount else []
    if rules.min_cost:
        minimum.append(money(rules.min_cost))
    print(
        f"  Tick harga {money(rules.tick) if settings.quote in ('IDR', 'BIDR', 'IDRT') else fmt_amount(rules.tick)}, "
        f"lot {fmt_amount(rules.step)} {rules.base}" + (f", minimal order {' / '.join(minimum)}" if minimum else "")
    )
    if bid is None or ask is None:
        print("  MASALAH: order book kosong")
        return 1
    print(f"  Harga sekarang: bid {money(bid)} | ask {money(ask)}")
    if candles:
        c = candles[-1]
        print(f"  Candle 1 menit terakhir: tertinggi {money(c.high)}, terendah {money(c.low)}")
    else:
        print("  Belum ada candle 1 menit dalam 10 menit terakhir (pasangan sepi?)")

    print("\n== 3. Rencana grid ==")
    g, costs = settings.grid, settings.costs
    price = (bid + ask) / 2
    if g.lower_price > 0:
        lower, upper = g.lower_price, g.upper_price
        how = "dari config"
    else:
        lower, upper = auto_range(price, g.range_pct)
        how = f"otomatis ±{g.range_pct:g}% dari harga sekarang"
    plan = plan_grid(lower, upper, g, costs, rules, settings.risk.paper_balance)
    print(f"  Range {money(lower)} – {money(upper)} ({how})")
    print(f"  {g.levels} level = {g.levels - 1} slot × {money(g.order_value)} per order beli")
    if plan.slots:
        print(
            f"  Jarak per level {plan.min_gap_pct:.2f}% | biaya + pajak per putaran {plan.round_trip_cost_pct:.2f}% "
            f"| untung bersih per putaran ±{plan.net_profit_pct:.2f}%"
        )
        print(
            f"  Modal terpakai kalau semua order beli terisi: {money(plan.capital)} "
            f"(modal paper {money(settings.risk.paper_balance)})"
        )
        print("  Level: " + ", ".join(money(level) for level in plan.levels))
    if settings.risk.stop_loss_pct > 0:
        print(f"  Stop-loss: jual semua kalau harga di bawah {money(lower * (1 - settings.risk.stop_loss_pct / 100))}")
    else:
        print("  Stop-loss: mati")
    if plan.problems:
        ok = False
        for problem in plan.problems:
            print(f"  MASALAH: {problem}")
    saved = JsonStore(os.path.join(settings.data_dir, f"state_{MODE}.json")).load()
    if saved is not None:
        print(
            f"  Catatan: sudah ada grid tersimpan ({saved.get('symbol')}, mulai {saved.get('started_at')}). "
            "Bot memakai grid itu selama pengaturan grid tidak berubah; reset: python -m spot.main --reset"
        )

    print("\n== 4. Telegram ==")
    n = settings.notifications
    notifier = Notifier(n.telegram_bot_token, n.telegram_chat_id)
    if not notifier.enabled:
        print("  Mati (isi TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID di .env untuk menyalakan)")
    elif args.telegram_test:
        try:
            notifier.send_now(f"✅ Tes dari bot grid Tokocrypto ({settings.symbol}) — Telegram tersambung.")
            print("  OK: pesan tes terkirim, cek Telegram kamu")
        except NotificationError as exc:
            print(f"  GAGAL mengirim: {exc}")
            ok = False
        except requests.RequestException as exc:
            print(f"  GAGAL mengirim ({type(exc).__name__})")
            ok = False
        finally:
            notifier.close()
    else:
        print("  Aktif (tambahkan --telegram-test untuk mengirim pesan tes)")
        notifier.close()

    print("\n" + ("✅ Siap: jalankan bot dengan python -m spot.main" if ok else "❌ Perbaiki masalah di atas dulu."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
