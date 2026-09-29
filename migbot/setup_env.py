"""`python -m migbot setup`: put Telegram and the Solana RPC into .env, tested on the spot.

Asks for the bot token from @BotFather (hidden input), finds the chat id by
itself once the user has messaged the bot, sends a test message, then asks
for an RPC URL (e.g. Helius) and checks that it can read token holders.
Only values that passed their test are written; other .env lines are kept.
"""
from __future__ import annotations

import getpass
import os
import re
import shutil

import requests

from migbot.http import redact, register_secret

TOKEN_KEY, CHAT_KEY, RPC_KEY = "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SOLANA_RPC_URL"
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def set_env_values(text: str, values: dict[str, str]) -> str:
    """Set KEY=value lines: the first existing line is replaced, duplicates removed, new keys appended."""
    lines = text.splitlines()
    for key, value in values.items():
        pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
        found = [i for i, line in enumerate(lines) if pattern.match(line)]
        if found:
            lines[found[0]] = f"{key}={value}"
            for i in reversed(found[1:]):
                del lines[i]
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def mask_token(token: str) -> str:
    return f"{token[:4]}…{token[-4:]}" if len(token) > 12 else "…"


def mask_url(url: str) -> str:
    head, sep, key = url.partition("api-key=")
    return f"{head}{sep}{key[:4]}…" if sep else (url[:40] + ("…" if len(url) > 40 else ""))


def chats_from_updates(payload) -> list[tuple[int, str]]:
    """(chat id, name) of every chat that messaged the bot, newest first, without duplicates."""
    out: list[tuple[int, str]] = []
    seen = set()
    results = payload.get("result") if isinstance(payload, dict) else None
    for update in reversed(results or []):
        for kind in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(kind) or {}).get("chat") if isinstance(update, dict) else None
            if not isinstance(chat, dict) or "id" not in chat or chat["id"] in seen:
                continue
            seen.add(chat["id"])
            person = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
            name = chat.get("title") or person or chat.get("username") or "?"
            out.append((chat["id"], name))
    return out


class TelegramError(RuntimeError):
    def __init__(self, message: str, network: bool = False):
        super().__init__(redact(message))
        self.network = network


class TelegramApi:
    base = "https://api.telegram.org"

    def _call(self, token: str, method: str, **params):
        try:
            resp = requests.post(f"{self.base}/bot{token}/{method}", data=params, timeout=15)
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise TelegramError(f"{type(exc).__name__}", network=True) from None
        if not data.get("ok"):
            raise TelegramError(data.get("description") or f"HTTP {resp.status_code}")
        return data

    def get_me(self, token: str) -> str:
        return self._call(token, "getMe")["result"].get("username") or "?"

    def get_updates(self, token: str) -> dict:
        return self._call(token, "getUpdates", timeout=0)

    def send(self, token: str, chat_id, text: str) -> None:
        self._call(token, "sendMessage", chat_id=chat_id, text=text)


def check_rpc(url: str) -> tuple[bool, str]:
    """Can this RPC read the largest holders of a token (the call public RPCs refuse)?"""
    from migbot.check import SAMPLE_MINT
    from migbot.http import JsonHttp
    from migbot.sources.solana_rpc import SolanaRpc

    rpc = SolanaRpc(url, http=JsonHttp("RPC", retries=1, timeout=20))
    try:
        rpc.call("getVersion", [])
        rows = rpc.largest_accounts(SAMPLE_MINT)
    except Exception as exc:  # noqa: BLE001 - any failure is reported to the user
        return False, redact(exc)[:200]
    if not rows:
        return False, "RPC menjawab kosong"
    return True, f"bisa membaca {len(rows)} holder terbesar"


def _setup_telegram(ask, ask_secret, tg: TelegramApi, out) -> dict[str, str]:
    out("")
    out("[1/2] Telegram")
    for _ in range(3):
        out("  Tempel token dari @BotFather (klik kanan), lalu Enter. Tulisannya memang tidak")
        token = ask_secret("  terlihat saat ditempel. Kosong = lewati: ").strip()
        if not token:
            out("  Telegram dilewati.")
            return {}
        if not TOKEN_RE.match(token):
            out("  ✕ Format token salah. Bentuknya seperti 123456789:AAH... (angka, titik dua, huruf). Coba lagi.")
            continue
        register_secret(token)
        try:
            username = tg.get_me(token)
        except RuntimeError as exc:
            if getattr(exc, "network", False):
                out(f"  ✕ VPS tidak bisa menghubungi Telegram ({exc}). Cek internet VPS, lalu jalankan setup lagi.")
                return {}
            out(f"  ✕ Token ditolak Telegram ({exc}). Salin ulang token dari @BotFather, lalu coba lagi.")
            continue
        out(f"  ✓ Token benar ({mask_token(token)}): bot @{username}")
        for _ in range(5):
            ask(f"  Di Telegram, buka @{username}, tekan START atau kirim 'halo', lalu tekan Enter di sini... ")
            try:
                chats = chats_from_updates(tg.get_updates(token))
            except RuntimeError as exc:
                out(f"  ✕ Gagal membaca pesan bot: {exc}")
                continue
            if not chats:
                out(f"  Belum ada pesan untuk @{username}. Pastikan pesannya dikirim ke bot itu, lalu coba lagi.")
                continue
            chat_id, name = chats[0]
            if len(chats) > 1:
                for i, (cid, cname) in enumerate(chats, 1):
                    out(f"    {i}. {cname} (id {cid})")
                pick = ask("  Pilih nomor chat untuk notifikasi [1]: ").strip() or "1"
                if pick.isdigit() and 1 <= int(pick) <= len(chats):
                    chat_id, name = chats[int(pick) - 1]
            try:
                tg.send(token, chat_id, "✅ Telegram tersambung ke migbot (bot meme coin migrated).")
            except RuntimeError as exc:
                out(f"  ✕ Pesan tes gagal dikirim: {exc}")
                return {}
            out(f"  ✓ Chat: {name} (id {chat_id}). Pesan tes terkirim, cek Telegram-mu.")
            return {TOKEN_KEY: token, CHAT_KEY: str(chat_id)}
        out("  ✕ Chat tidak ditemukan. Jalankan perintah ini lagi setelah mengirim pesan ke bot.")
        return {}
    out("  ✕ Telegram belum tersimpan.")
    return {}


def _setup_rpc(ask_secret, rpc_check, out) -> dict[str, str]:
    out("")
    out("[2/2] RPC Solana (misalnya Helius, gratis)")
    for _ in range(3):
        out("  Tempel RPC URL (https://mainnet.helius-rpc.com/?api-key=...), lalu Enter.")
        url = ask_secret("  Tulisannya memang tidak terlihat. Kosong = lewati: ").strip()
        if not url:
            out("  RPC dilewati (bot memakai RPC publik yang sering menolak cek holder).")
            return {}
        if not url.startswith(("https://", "http://")) or " " in url:
            out("  ✕ URL harus diawali https:// dan tanpa spasi. Coba lagi.")
            continue
        register_secret(url)
        out(f"  Mengetes {mask_url(url)} ...")
        ok, detail = rpc_check(url)
        if ok:
            out(f"  ✓ RPC berfungsi: {detail}")
            return {RPC_KEY: url}
        out(f"  ✕ RPC belum bisa dipakai: {detail}. Cek URL-nya, lalu coba lagi.")
    out("  ✕ RPC belum tersimpan.")
    return {}


def run_setup(env_path: str, ask=input, ask_secret=getpass.getpass, tg: TelegramApi | None = None, rpc_check=check_rpc, out=print) -> int:
    tg = tg or TelegramApi()
    if not os.path.exists(env_path):
        example = os.path.join(os.path.dirname(os.path.abspath(env_path)), ".env.example")
        if os.path.exists(example):
            shutil.copyfile(example, env_path)
        else:
            open(env_path, "w", encoding="utf-8").close()
    out(f"Pengaturan migbot: {os.path.abspath(env_path)}")
    values = {}
    values.update(_setup_telegram(ask, ask_secret, tg, out))
    values.update(_setup_rpc(ask_secret, rpc_check, out))
    out("")
    if not values:
        out("Tidak ada yang diubah.")
        return 0
    with open(env_path, encoding="utf-8") as fh:
        text = fh.read()
    tmp = f"{env_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(set_env_values(text, values))
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)
    out(f"✅ Disimpan ke .env: {', '.join(values)}")
    out("Supaya bot memakai pengaturan baru, restart bot: systemctl restart migbot")
    return 0
