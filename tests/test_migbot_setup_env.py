"""The .env wizard: python -m migbot setup."""
import os
import stat

from migbot.setup_env import CHAT_KEY, RPC_KEY, TOKEN_KEY, chats_from_updates, mask_url, run_setup, set_env_values

TOKEN = "123456789:AAH" + "x" * 32
RPC = "https://mainnet.helius-rpc.com/?api-key=abcd1234-secret"


def test_set_env_values_replaces_dedupes_and_appends():
    text = "# comment\nTELEGRAM_BOT_TOKEN=\nPOLY_X=1\nexport TELEGRAM_BOT_TOKEN=old\nSOLANA_RPC_URL=\n"
    out = set_env_values(text, {TOKEN_KEY: "t", RPC_KEY: "u", CHAT_KEY: "42"})
    assert out == "# comment\nTELEGRAM_BOT_TOKEN=t\nPOLY_X=1\nSOLANA_RPC_URL=u\nTELEGRAM_CHAT_ID=42\n"


def test_chats_from_updates_newest_first_unique():
    payload = {"ok": True, "result": [
        {"update_id": 1, "message": {"chat": {"id": 11, "first_name": "Budi", "last_name": "S"}, "text": "/start"}},
        {"update_id": 2, "my_chat_member": {"chat": {"id": -500, "title": "Grup Bot"}}},
        {"update_id": 3, "message": {"chat": {"id": 11, "first_name": "Budi"}, "text": "halo"}},
        {"update_id": 4, "weird": {}},
    ]}
    assert chats_from_updates(payload) == [(11, "Budi"), (-500, "Grup Bot")]
    assert chats_from_updates({"ok": True, "result": []}) == [] and chats_from_updates(None) == []


class FakeTg:
    def __init__(self, updates):
        self.updates = list(updates)
        self.sent = []

    def get_me(self, token):
        if token != TOKEN:
            raise RuntimeError("Unauthorized")
        return "migbot_test_bot"

    def get_updates(self, token):
        return self.updates.pop(0) if self.updates else {"ok": True, "result": []}

    def send(self, token, chat_id, text):
        self.sent.append((chat_id, text))


def run(tmp_path, secrets, answers, updates, rpc_results):
    env = tmp_path / ".env"
    (tmp_path / ".env.example").write_text("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\nSOLANA_RPC_URL=\nOTHER=keep\n", encoding="utf-8")
    printed = []
    secrets, answers, rpc_results = list(secrets), list(answers), list(rpc_results)
    tg = FakeTg(updates)
    code = run_setup(
        str(env), ask=lambda prompt="": answers.pop(0), ask_secret=lambda prompt="": secrets.pop(0),
        tg=tg, rpc_check=lambda url: rpc_results.pop(0), out=printed.append,
    )
    return code, env, "\n".join(printed), tg


def test_full_setup_with_retries(tmp_path):
    empty = {"ok": True, "result": []}
    with_chat = {"ok": True, "result": [{"message": {"chat": {"id": 777, "first_name": "Budi"}}}]}
    code, env, printed, tg = run(
        tmp_path,
        secrets=["not-a-token", "123456789:AAH" + "y" * 32, TOKEN, "ftp://bad", RPC, RPC],
        answers=["", ""],  # Enter after messaging the bot: first no message yet, then found
        updates=[empty, with_chat],
        rpc_results=[(False, "HTTP 429"), (True, "bisa membaca 20 holder terbesar")],
    )
    assert code == 0
    text = env.read_text(encoding="utf-8")
    assert f"{TOKEN_KEY}={TOKEN}\n" in text and f"{CHAT_KEY}=777\n" in text and f"{RPC_KEY}={RPC}\n" in text
    assert "OTHER=keep" in text
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    assert tg.sent and tg.sent[0][0] == 777
    assert "Format token salah" in printed and "Token ditolak Telegram (Unauthorized)" in printed
    assert "Belum ada pesan" in printed and "HTTP 429" in printed
    assert TOKEN not in printed and "abcd1234-secret" not in printed  # secrets never echoed


def test_everything_skipped_changes_nothing(tmp_path):
    code, env, printed, _ = run(tmp_path, secrets=["", ""], answers=[], updates=[], rpc_results=[])
    assert code == 0 and "Tidak ada yang diubah" in printed
    assert env.read_text(encoding="utf-8").startswith("TELEGRAM_BOT_TOKEN=\n")  # copied from .env.example


def test_choose_between_several_chats(tmp_path):
    chats = {"ok": True, "result": [
        {"message": {"chat": {"id": 1, "first_name": "Satu"}}},
        {"message": {"chat": {"id": -2, "title": "Grup"}}},
    ]}
    code, env, printed, tg = run(tmp_path, secrets=[TOKEN, ""], answers=["", "2"], updates=[chats], rpc_results=[])
    assert f"{CHAT_KEY}=1\n" in env.read_text(encoding="utf-8")  # newest first: 2 = "Satu"
    assert tg.sent[0][0] == 1


def test_mask_url():
    assert mask_url(RPC) == "https://mainnet.helius-rpc.com/?api-key=abcd…"


# ---------------------------------------------------------------- secrets never reach logs or messages
def test_redact_patterns_and_registered_secrets():
    from migbot.http import HttpError, redact, register_secret

    msg = "ConnectionError: HTTPSConnectionPool(host='mainnet.helius-rpc.com'): Max retries exceeded with url: /?api-key=0f1e2d3c-aaaa-bbbb-cccc-1234567890ab"
    assert "0f1e2d3c" not in redact(msg) and "api-key=***" in redact(msg)
    tg = "https://api.telegram.org/bot123456789:AAH" + "q" * 32 + "/sendMessage"
    assert redact(tg) == "https://api.telegram.org/bot***/sendMessage"
    assert "AAH" not in redact("token 123456789:AAH" + "q" * 32 + " is wrong")
    path_key = "https://solana-mainnet.example.pro/9f8e7d6c5b4a39281706f5e4d3c2b1a0/"
    register_secret(path_key)
    assert "9f8e7d6c5b4a" not in redact(f"GET {path_key} failed") and "9f8e7d6c5b4a" not in str(HttpError(f"boom {path_key}"))
    assert redact("DexScreener $BONK likuiditas $416.5k") == "DexScreener $BONK likuiditas $416.5k"


def test_log_lines_and_tracebacks_are_redacted(tmp_path):
    import logging

    from migbot.logger import RedactingFormatter

    record = logging.LogRecord("migbot.x", logging.WARNING, __file__, 1, "RPC failed: %s", ("url /?api-key=SECRET123456",), None)
    try:
        raise RuntimeError("inside /bot123456789:AAH" + "w" * 32 + "/getMe")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()
    text = RedactingFormatter("%(message)s").format(record)
    assert "SECRET123456" not in text and "AAHwww" not in text and "Traceback" in text


def test_setup_network_error_does_not_echo_the_token(tmp_path):
    from migbot.setup_env import TelegramError

    class Offline(FakeTg):
        def get_me(self, token):
            raise TelegramError(f"ProxyError /bot{token}/getMe", network=True)

    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
    printed = []
    secrets = [TOKEN, ""]
    run_setup(str(env), ask=lambda p="": "", ask_secret=lambda p="": secrets.pop(0), tg=Offline([]),
              rpc_check=lambda u: (True, ""), out=printed.append)
    text = "\n".join(printed)
    assert "tidak bisa menghubungi Telegram" in text and TOKEN not in text and TOKEN[10:] not in text
