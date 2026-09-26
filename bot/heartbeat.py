"""Polymarket's dead-man switch for resting orders.

Once the exchange has accepted a first heartbeat, it cancels every open
order of the account if it doesn't receive another within about 10 seconds.
While the bot market-makes live, a background thread sends one every 5
seconds; if the bot, its server or its network dies, the quotes are pulled
by the exchange instead of sitting there to be picked off.

The exchange tracks a heartbeat id: an outdated or empty one is answered
with 400 Bad Request carrying the current id, which is adopted and retried.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("polybot.heartbeat")

# The exchange's timeout; after this long without a successful heartbeat the
# account's orders must be assumed cancelled.
EXCHANGE_TIMEOUT_SECONDS = 10.0


def _heartbeat_id_in(payload) -> str | None:
    if isinstance(payload, dict):
        value = payload.get("heartbeat_id") or payload.get("heartbeatId")
        if isinstance(value, str) and value:
            return value
    return None


class Heartbeat:
    def __init__(self, client, interval: float = 5.0):
        self.client = client
        self.interval = interval
        self._id = ""
        self._last_ok = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def healthy(self) -> bool:
        """Whether the exchange heard from us recently enough to keep our orders."""
        return time.monotonic() - self._last_ok < EXCHANGE_TIMEOUT_SECONDS

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def beat(self) -> bool:
        """Send one heartbeat (adopting a corrected id once). True on success."""
        for _ in range(2):
            try:
                response = self.client.post_heartbeat(self._id)
            except Exception as exc:
                corrected = _heartbeat_id_in(getattr(exc, "error_msg", None))
                if corrected and corrected != self._id:
                    self._id = corrected
                    continue
                logger.warning("Heartbeat failed (%s)", type(exc).__name__)
                return False
            self._id = _heartbeat_id_in(response) or self._id
            self._last_ok = time.monotonic()
            return True
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            self.beat()
            self._stop.wait(self.interval)
