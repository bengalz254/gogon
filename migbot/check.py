"""`python -m migbot check`: settings summary and a live test of every data source."""
from __future__ import annotations

import json
import threading
import time

from migbot.config import SOL_MINT, Settings
from migbot.engine import Sources
from migbot.filters import fmt_dur, fmt_usd
from migbot.http import redact
from migbot.notifier import Notifier

SAMPLE_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # BONK: old, liquid, always listed


def _line(tag: str, name: str, detail: str) -> None:
    print(f"[{tag}] {name:<14} {redact(detail)}")


def _pumpportal(s: Settings, wait_s: float = 15.0) -> bool:
    try:
        import websocket  # websocket-client
    except ImportError:
        _line("GAGAL", "PumpPortal", "paket websocket-client belum terpasang (pip install -r requirements-migbot.txt)")
        return False
    url = s.discovery.pumpportal.ws_url
    if s.pumpportal_api_key:
        url += ("&" if "?" in url else "?") + f"api-key={s.pumpportal_api_key}"
    got: list[str] = []
    done = threading.Event()

    def on_open(ws):
        ws.send(json.dumps({"method": "subscribeMigration"}))

    def on_message(ws, message):
        got.append(message)
        done.set()

    def on_error(ws, error):
        got.append(f"ERROR {error}")
        done.set()

    app = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error)
    thread = threading.Thread(target=app.run_forever, daemon=True)
    thread.start()
    done.wait(wait_s)
    app.close()
    if not got:
        _line("!", "PumpPortal", f"tersambung? tidak ada balasan dalam {wait_s:.0f} detik")
        return False
    if got[0].startswith("ERROR"):
        _line("GAGAL", "PumpPortal", got[0][6:200])
        return False
    _line("OK", "PumpPortal", f"subscribeMigration dijawab: {got[0][:120]}")
    return True


def run_check(s: Settings, telegram_test: bool = False) -> int:
    f = s.filters
    print("Konfigurasi:", s.config_path)
    print(
        f"  beli {s.trading.buy_sol:g} SOL, {fmt_dur(s.entry.delay_seconds)} sampai {fmt_dur(s.entry.window_seconds)} setelah migrasi, "
        f"saldo paper {s.trading.paper_balance_sol:g} SOL, maks {s.trading.max_open_positions} posisi"
    )
    print(
        f"  filter: likuiditas ≥ {fmt_usd(f.min_liquidity_usd)}, mcap {fmt_usd(f.min_market_cap_usd)}-"
        f"{fmt_usd(f.max_market_cap_usd) if f.max_market_cap_usd else '∞'}, vol 5m ≥ {fmt_usd(f.min_volume_5m_usd)}, "
        f"txn 5m ≥ {f.min_txns_5m}, beli ≥ {f.min_buy_ratio_5m * 100:g}%, top10 ≤ {f.max_top10_pct:g}%, "
        f"dev ≤ {f.max_dev_hold_pct:g}%"
    )
    print(f"  RPC: {s.rpc_url.split('?')[0]}{' (+ key)' if '?' in s.rpc_url else ''}")
    print()
    src = Sources.build(s)
    problems = 0
    discovery_ok = False

    if s.discovery.pumpportal.enabled:
        discovery_ok |= _pumpportal(s)
    sample = SAMPLE_MINT
    if src.gecko is not None:
        try:
            events = src.gecko.poll()
            discovery_ok = True
            recent = ", ".join(e.symbol or e.mint[:8] for e in events[:5]) or "belum ada di halaman terbaru"
            _line("OK", "GeckoTerminal", f"{len(events)} pool {'/'.join(s.discovery.geckoterminal.dexes)} baru: {recent}")
            if events:
                sample = events[0].mint
        except Exception as exc:  # noqa: BLE001
            _line("!", "GeckoTerminal", f"gagal: {exc}")
    if not discovery_ok:
        problems += 1
        _line("GAGAL", "Deteksi", "tidak ada sumber migrasi yang jalan; bot tidak akan menemukan token")

    snap = None
    try:
        snaps = src.dexscreener.fetch([sample])
        snap = snaps.get(sample)
        if snap is None and sample != SAMPLE_MINT:
            sample = SAMPLE_MINT
            snap = src.dexscreener.fetch([sample]).get(sample)
        if snap is None:
            _line("!", "DexScreener", "tersambung, tapi token contoh tidak punya pair SOL")
        else:
            _line("OK", "DexScreener", f"${snap.symbol}: harga ${snap.price_usd}, likuiditas {fmt_usd(snap.liquidity_usd)}")
    except Exception as exc:  # noqa: BLE001
        problems += 1
        _line("GAGAL", "DexScreener", f"{exc} (wajib: sumber harga)")

    try:
        version = src.rpc.call("getVersion", [])
        pair = snap.pair_address if snap else ""
        report = src.rpc.safety_report(sample, pair, s.safety.amm_owners)
        detail = (
            f"versi {version.get('solana-core', '?') if isinstance(version, dict) else version}; contoh: "
            f"top10 {'?' if report.top10_pct is None else f'{report.top10_pct:.1f}%'}, "
            f"mint authority {'dicabut' if report.authorities_known and not report.mint_authority else '?/aktif'}"
        )
        if report.errors:
            _line("!", "Solana RPC", detail + " | sebagian gagal: " + "; ".join(report.errors)[:200])
        else:
            _line("OK", "Solana RPC", detail)
    except Exception as exc:  # noqa: BLE001
        _line("!", "Solana RPC", f"{exc} — cek keamanan (holder/dev/authority) akan dilewati. Isi SOLANA_RPC_URL (mis. Helius gratis)")

    if src.rugcheck is not None:
        try:
            rug = src.rugcheck.fetch(sample)
            _line("OK", "RugCheck", f"skor {rug.score_normalised}, {len(rug.risks)} catatan")
        except Exception as exc:  # noqa: BLE001
            _line("!", "RugCheck", f"{exc} — filter RugCheck akan dilewati")
    if src.gmgn is not None:
        try:
            info = src.gmgn.fetch(sample)
            _line("OK", "GMGN", f"holder {info.holders}, smart money beli {info.smart_buys}")
        except Exception as exc:  # noqa: BLE001
            _line("!", "GMGN", f"{exc} — bot tetap jalan; filter khusus GMGN dilewati")
    if src.jupiter is not None:
        try:
            quote = src.jupiter.quote(SOL_MINT, SAMPLE_MINT, 10_000_000)
            _line("OK", "Jupiter", f"0.01 SOL -> {quote.out_amount} unit BONK (rute {quote.route or '?'})")
        except Exception as exc:  # noqa: BLE001
            _line("!", "Jupiter", f"{exc} — simulasi harga memakai estimasi fee + slippage")

    notifier = Notifier(s.telegram_token, s.telegram_chat_id, enabled=False)
    if telegram_test:
        ok, detail = notifier.send_now("Tes notifikasi bot meme coin migrated: berhasil ✅")
        _line("OK" if ok else "!", "Telegram", "pesan tes terkirim" if ok else f"gagal: {detail}")
    elif s.telegram_token and s.telegram_chat_id:
        _line("OK", "Telegram", "diisi (tes kirim: python -m migbot check --telegram-test)")
    else:
        _line("!", "Telegram", "belum diisi di .env (opsional)")

    print()
    if problems:
        print(f"MASALAH: {problems} sumber wajib gagal. Perbaiki dulu sebelum menjalankan bot.")
        return 1
    print(f"✅ Siap. Jalankan: python -m migbot run   ({time.strftime('%H:%M:%S')})")
    return 0
