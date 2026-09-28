"""Optional Telegram notifications.

Messages are sent from a background thread so a slow or unreachable Telegram
API can never delay order handling. If the queue fills up, the oldest
messages are dropped rather than blocking the bot.
"""
from __future__ import annotations

import logging
import queue
import threading

import requests

logger = logging.getLogger("scalper.notify")


class Notifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None, prefix: str = "", enabled: bool = True):
        self.enabled = bool(enabled and token and chat_id)
        self.token = token
        self.chat_id = chat_id
        self.prefix = prefix
        self._q: queue.Queue[str | None] = queue.Queue(maxsize=100)
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
                    url,
                    data={"chat_id": self.chat_id, "text": msg, "disable_web_page_preview": "true"},
                    timeout=10,
                )
                if r.status_code != 200:
                    logger.warning("Telegram send failed: HTTP %s %s", r.status_code, r.text[:200])
            except requests.RequestException as e:
                logger.warning("Telegram send failed: %s", e)
