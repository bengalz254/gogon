"""Entry point: python -m spot.main   (Tokocrypto grid bot, paper mode)

    python -m spot.main            run the bot
    python -m spot.main --reset    archive the paper grid; the next run starts fresh

Settings: config/spot.yaml. Telegram (optional): TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID in .env.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time

from bot.logger import setup_logging
from bot.notify import Notifier
from spot.bot import MODE, GridBot, StartupError, write_error_status
from spot.config import ConfigError, SpotSettings, load_settings
from spot.market import SymbolError, TokocryptoData
from spot.records import JsonStore

logger = logging.getLogger("spot.main")


def _wait_on_error() -> bool:
    """In Docker (SPOT_WAIT_ON_ERROR=1) a bot that can't start waits for
    `docker compose stop` instead of exiting, so it isn't restarted over and
    over with the same error."""
    return os.getenv("SPOT_WAIT_ON_ERROR", "").strip().lower() in ("1", "true", "yes")


def _idle_until_signal() -> None:
    stop = []
    signal.signal(signal.SIGINT, lambda *_: stop.append(True))
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
    while not stop:
        time.sleep(1.0)


def bot_seems_running(settings: SpotSettings, now: float | None = None) -> bool:
    """True if data/spot/status.json says the bot is up and was updated recently."""
    now = time.time() if now is None else now
    try:
        with open(os.path.join(settings.data_dir, "status.json"), encoding="utf-8") as f:
            status = json.load(f)
        if status.get("running") is not True:
            return False
        return now - float(status["updated_ts"]) < max(120.0, 4 * settings.poll_seconds)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def reset(settings: SpotSettings, force: bool = False) -> int:
    if bot_seems_running(settings) and not force:
        print("Bot grid sepertinya masih jalan. Hentikan dulu (docker compose stop spot), lalu reset lagi.")
        print("Kalau yakin bot sudah mati, tambahkan --force.")
        return 1
    backup = JsonStore(os.path.join(settings.data_dir, f"state_{MODE}.json")).archive()
    if backup is None:
        print("Belum ada grid tersimpan; bot akan membuat grid baru saat dijalankan.")
    else:
        print(f"Grid paper lama disimpan sebagai {backup}.")
        print("Saat dijalankan lagi, bot membuat grid baru di sekitar harga terkini dengan modal dari config.")
        print("Riwayat transaksi (trades.csv) tetap disimpan.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tokocrypto grid bot (paper mode)")
    parser.add_argument("--config", help="path to the settings file (default: config/spot.yaml)")
    parser.add_argument("--reset", action="store_true", help="archive the saved paper grid so the next run starts fresh")
    parser.add_argument("--force", action="store_true", help="with --reset: even if the bot looks like it's running")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"Config salah: {exc}", file=sys.stderr)
        if _wait_on_error():
            print("Perbaiki config/spot.yaml lalu jalankan ulang (docker compose restart spot).", file=sys.stderr)
            _idle_until_signal()
        return 2

    if args.reset:
        return reset(settings, force=args.force)

    setup_logging(name="spot", filename="spot.log")
    if os.getenv("LIVE_TRADING", "").strip().lower() in ("1", "true", "yes", "on"):
        logger.warning("LIVE_TRADING is ignored: the Tokocrypto grid bot only runs in paper mode.")
    notifier = Notifier(settings.notifications.telegram_bot_token, settings.notifications.telegram_chat_id)
    if notifier.enabled:
        logger.info("Telegram notifications enabled.")
    bot = GridBot(settings, TokocryptoData(settings.symbol), notifier)
    signal.signal(signal.SIGINT, bot.request_stop)
    signal.signal(signal.SIGTERM, bot.request_stop)
    try:
        return bot.run()
    except (SymbolError, StartupError) as exc:
        logger.error("The grid can't start:\n%s", exc)
        notifier.send(f"⛔ Bot grid tidak bisa mulai ({settings.symbol}):\n{exc}")
        write_error_status(bot.status_path, str(exc))
        if _wait_on_error():
            logger.error("Fix config/spot.yaml, then: docker compose restart spot")
            bot.idle_until_stopped()
        notifier.close()
        return 2


if __name__ == "__main__":
    sys.exit(main())
