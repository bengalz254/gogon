"""Telegram alerts for a bot that runs unattended.

Best effort by design: messages go through a small background queue so a
slow or unreachable Telegram never stalls trading, delivery failures are
only logged, and the bot token (which is part of the API URL) is never
written to the logs. Without TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env
every call is a no-op.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

import requests

logger = logging.getLogger("polybot.notify")

TELEGRAM_API = "https://api.telegram.org"
_MAX_MESSAGE_CHARS = 4000  # Telegram rejects messages over 4096 characters


class NotificationError(Exception):
    """A delivery failure whose message is safe to log (contains no secrets)."""


class Notifier:
    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        sender: Callable[[str], None] | None = None,
    ):
        self.enabled = bool(bot_token and chat_id)
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._sender = sender or self._send_telegram
        self._last_sent: dict[str, float] = {}
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=200)
        self._thread: threading.Thread | None = None
        if self.enabled:
            self._thread = threading.Thread(target=self._worker, name="notifier", daemon=True)
            self._thread.start()

    def send(self, text: str, key: str | None = None, cooldown_s: float = 0.0) -> bool:
        """Queue `text` for delivery. Returns whether it was queued.

        Messages with the same `key` are sent at most once per `cooldown_s`
        seconds, so a problem that repeats every cycle alerts once rather
        than flooding the chat.
        """
        if not self.enabled:
            return False
        if key is not None:
            now = time.monotonic()
            last = self._last_sent.get(key)
            if last is not None and now - last < cooldown_s:
                return False
            self._last_sent[key] = now
        try:
            self._queue.put_nowait(text[:_MAX_MESSAGE_CHARS])
        except queue.Full:
            logger.warning("Notification queue full; dropping a message")
            return False
        return True

    def send_now(self, text: str) -> None:
        """Send synchronously and raise on failure (for setup checks, not the trading loop)."""
        if not self.enabled:
            raise NotificationError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        self._sender(text[:_MAX_MESSAGE_CHARS])

    def close(self, timeout: float = 10.0) -> None:
        """Deliver what's queued (waiting up to `timeout` seconds), then stop."""
        if self._thread is None:
            return
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            return
        self._thread.join(timeout)
        self._thread = None

    def _worker(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                return
            try:
                self._sender(text)
            except NotificationError as exc:
                logger.warning("Telegram notification failed: %s", exc)
            except Exception as exc:  # never let an alert take anything down
                # Only the type: request errors embed the URL, which holds the token.
                logger.warning("Telegram notification failed (%s)", type(exc).__name__)

    def _send_telegram(self, text: str) -> None:
        resp = requests.post(
            f"{TELEGRAM_API}/bot{self._bot_token}/sendMessage",
            json={"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        if resp.status_code != 200:
            raise NotificationError(f"HTTP {resp.status_code}")
