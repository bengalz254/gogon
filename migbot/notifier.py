"""Telegram alerts (optional) and the message texts, in Indonesian.

Messages go out from a background thread, so a slow or unreachable
Telegram can never hold up the bot. Plain text, no Markdown to escape.
"""
from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone

import requests

from migbot.filters import fmt_dur, fmt_usd
from migbot.http import redact

logger = logging.getLogger("migbot.telegram")


class Notifier:
    def __init__(self, token: str = "", chat_id: str = "", enabled: bool = True, prefix: str = "[migbot] "):
        self.enabled = bool(enabled and token and chat_id)
        self.token, self.chat_id, self.prefix = token, chat_id, prefix
        self.sent = 0
        self.last_error = ""
        self._q: "queue.Queue[str | None]" = queue.Queue(maxsize=200)
        self._thread: threading.Thread | None = None
        if self.enabled:
            self._thread = threading.Thread(target=self._worker, name="telegram", daemon=True)
            self._thread.start()

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        msg = f"{self.prefix}{text}"[:4000]
        try:
            self._q.put_nowait(msg)
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(msg)
            except (queue.Empty, queue.Full):
                pass

    def send_now(self, text: str) -> tuple[bool, str]:
        """Synchronous send (for `check --telegram-test`)."""
        if not (self.token and self.chat_id):
            return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID belum diisi di .env"
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": f"{self.prefix}{text}", "disable_web_page_preview": "true"},
                timeout=10,
            )
        except requests.RequestException as exc:
            return False, redact(exc)
        return (r.status_code == 200), f"HTTP {r.status_code}"

    def close(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        try:
            self._q.put(None, timeout=1)
        except queue.Full:
            return
        self._thread.join(timeout)

    def _worker(self) -> None:
        session = requests.Session()
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        while True:
            msg = self._q.get()
            if msg is None:
                return
            try:
                r = session.post(
                    url, data={"chat_id": self.chat_id, "text": msg, "disable_web_page_preview": "true"}, timeout=10
                )
                if r.status_code == 200:
                    self.sent += 1
                else:
                    self.last_error = f"HTTP {r.status_code}"
                    logger.warning("Telegram send failed: HTTP %s %s", r.status_code, r.text[:200])
            except requests.RequestException as exc:
                self.last_error = redact(exc)[:200]
                logger.warning("Telegram send failed: %s", self.last_error)


# --------------------------------------------------------------------------- texts


def links(mint: str, pair: str = "") -> str:
    return (
        f"GMGN: https://gmgn.ai/sol/token/{mint}\n"
        f"DexScreener: https://dexscreener.com/solana/{pair or mint}\n"
        f"Mint: {mint}"
    )


def entry_rule(entry) -> str:
    """The entry rule in one line (Telegram start message and `migbot check`)."""
    when = f"{fmt_dur(entry.delay_seconds)} sampai {fmt_dur(entry.window_seconds)} setelah migrasi"
    if entry.dip_pct > 0:
        bounce = f", lalu naik lagi ≥ {entry.dip_bounce_pct:g}% dari dasar" if entry.dip_bounce_pct > 0 else ""
        return f"beli saat dip: harga ≥ {entry.dip_pct:g}% di bawah puncak sejak migrasi{bounce}; {when}"
    return f"beli begitu filter lolos; {when}"


def start_text(mode: str, balance: float, settings) -> str:
    f, x = settings.filters, settings.exits
    tps = ", ".join(f"+{g:g}% jual {p * 100:g}%" for g, p in x.take_profit) or "-"
    breakeven = f", impas setelah +{x.breakeven_after_pct:g}%" if x.breakeven_after_pct > 0 else ""
    research = ""
    if not settings.trading.enabled:
        research = "MODE RISET: tidak ada yang dibeli; token hanya dipantau dan dicatat.\n"
    if settings.long_tracking.enabled:
        research += f"Token yang masih hidup diikuti sampai {settings.long_tracking.max_days:g} hari (uji meme coin lama).\n"
    return (
        f"🟢 Bot migrated mulai ({mode}). Saldo paper {balance:.3f} SOL.\n"
        f"{research}"
        f"Beli {settings.trading.buy_sol:g} SOL, {entry_rule(settings.entry)}.\n"
        f"Filter: likuiditas ≥ {fmt_usd(f.min_liquidity_usd)}, mcap {fmt_usd(f.min_market_cap_usd)}-"
        f"{fmt_usd(f.max_market_cap_usd) if f.max_market_cap_usd else '∞'}, vol 5m ≥ {fmt_usd(f.min_volume_5m_usd)}, "
        f"top10 ≤ {f.max_top10_pct:g}%, dev ≤ {f.max_dev_hold_pct:g}%"
        f"{f', holder ≥ {f.min_holders}' if f.min_holders > 0 else ''}.\n"
        f"Maks {settings.trading.max_open_positions} posisi, {settings.trading.max_buys_per_day} beli/hari, "
        f"berhenti kalau rugi hari ini {settings.trading.max_daily_loss_sol:g} SOL.\n"
        f"Keluar: SL -{x.stop_loss_pct:g}%{breakeven}, TP {tps}, trailing {x.trailing_pct:g}% "
        f"(aktif +{x.trailing_start_pct:g}%), maks {x.max_hold_minutes:g} mnt."
    )


def migration_text(tok) -> str:
    return f"🆕 Migrasi: {tok.label} ({tok.source})\n{links(tok.mint, tok.pair_address)}"


def buy_text(tok, pos, fill, snap) -> str:
    ratio = snap.buy_ratio_m5
    safety = tok.safety or {}
    dev = safety.get("dev_pct")
    top10 = safety.get("top10_pct")
    holders = safety.get("holder_count")
    if holders is not None and not safety.get("holder_count_complete", True):
        holders = f"{holders}+"
    impact = f", impact {fill.impact_pct:.1f}%" if fill.impact_pct is not None else ""
    peak, low, price = tok.peak_price_usd, tok.dip_low_usd, snap.price_usd
    dip = ""
    if peak and low and price:
        dip = f" · {(1 - price / peak) * 100:.0f}% di bawah puncak, +{(price / low - 1) * 100:.0f}% dari dasar"
    return (
        f"✅ BELI (paper) {tok.label} {tok.name}\n"
        f"Umur {fmt_dur(pos.opened_at - tok.migrated_at)} sejak migrasi{dip}\n"
        f"Mcap {fmt_usd(snap.market_cap_usd)} · Likuiditas {fmt_usd(snap.liquidity_usd)} · "
        f"Vol 5m {fmt_usd(snap.volume_m5)} · Beli {'?' if ratio is None else f'{ratio * 100:.0f}%'}\n"
        f"Top10 {'?' if top10 is None else f'{top10:.0f}%'} · Dev {'?' if dev is None else f'{dev:.1f}%'}"
        f" · Holder {'?' if holders is None else holders}\n"
        f"Bayar {fill.sol:.4f} SOL ({fill.method}{impact})\n"
        f"{links(tok.mint, tok.pair_address)}"
    )


def sell_text(pos, fill, reason: str, pnl_sol: float, pnl_pct: float, closed: bool) -> str:
    icon = "💰" if pnl_sol >= 0 else "🔴"
    head = "TUTUP" if closed else "JUAL SEBAGIAN"
    total = f"\nTotal posisi: {pos.proceeds_sol - pos.cost_sol:+.4f} SOL" if closed else ""
    return (
        f"{icon} {head} (paper) ${pos.symbol} — {reason}\n"
        f"Terima {fill.sol:.4f} SOL ({fill.method}), P&L bagian ini {pnl_sol:+.4f} SOL ({pnl_pct:+.0f}%)"
        f"{total}\nGMGN: https://gmgn.ai/sol/token/{pos.mint}"
    )


def rejection_text(tok) -> str:
    reasons = "\n".join(f"- {r}" for r in tok.reasons[:6]) or "- tidak ada data pasar"
    return f"⛔ Tidak dibeli: {tok.label}\n{reasons}\nGMGN: https://gmgn.ai/sol/token/{tok.mint}"


def summary_text(day: str, stats: dict, balance: float, open_count: int, realized: float) -> str:
    return (
        f"📊 Ringkasan {day} (UTC)\n"
        f"Migrasi terdeteksi: {stats.get('migrations', 0)}\n"
        f"Dibeli: {stats.get('bought', 0)} · Ditolak: {stats.get('rejected', 0)}\n"
        f"Posisi ditutup: {stats.get('closed', 0)} (untung {stats.get('wins', 0)})\n"
        f"P&L terealisasi: {realized:+.4f} SOL\n"
        f"Saldo paper: {balance:.4f} SOL · posisi terbuka: {open_count}"
    )


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
