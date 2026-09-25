"""Reconnecting websocket loop shared by all feeds."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

logger = logging.getLogger("polybot.updown.feeds")


class WsFeed:
    """Connect, (re)subscribe, keep alive, reconnect with backoff.

    Subclasses implement `url()`, `on_open(ws)` and `on_message(raw)`.
    `on_message` returns True when the message carried real data. The idle
    watchdog only counts those: a socket that still answers PING with PONG
    (or sends empty frames) but no longer delivers prices is treated as dead
    and reconnected.
    """

    name = "ws"
    ping_text: str | None = "PING"  # application-level keepalive, sent as text
    ping_interval: float = 10.0
    idle_timeout: float = 60.0  # reconnect if no data arrives for this long
    # Protocol-level (control frame) pings from the websockets library. Off for
    # Polymarket sockets, which use text PING/PONG: on the real CLOB socket the
    # library's pings timed out about once a minute (close code 1011).
    protocol_ping: float | None = None

    def __init__(self, on_status: Callable[[str, bool, str], None] | None = None):
        self.on_status = on_status or (lambda feed, ok, detail: None)
        self.ws = None
        self.connected = False
        self.last_data = 0.0
        self.reconnects = 0

    def url(self) -> str:
        raise NotImplementedError

    async def on_open(self, ws) -> None:
        pass

    def on_message(self, raw) -> bool:
        raise NotImplementedError

    def on_close(self) -> None:
        """Called after every connection ends (for per-connection bookkeeping)."""

    def ready(self) -> bool:
        """Whether there is anything to connect for (e.g. tokens to watch)."""
        return True

    def _should_log(self) -> bool:
        # Reconnects can be frequent (e.g. while probing subscribe styles):
        # log the first few, then every 20th.
        return self.reconnects <= 3 or self.reconnects % 20 == 0

    async def run(self) -> None:
        import websockets

        backoff = 1.0
        while True:
            if not self.ready():
                await asyncio.sleep(1.0)
                continue
            detail = ""
            try:
                async with websockets.connect(
                    self.url(), open_timeout=20, ping_interval=self.protocol_ping,
                    ping_timeout=self.protocol_ping, max_size=2**24,
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    self.last_data = time.time()
                    await self.on_open(ws)
                    self.on_status(self.name, True, "")
                    if self._should_log():
                        logger.info("%s feed connected", self.name)
                    backoff = 1.0
                    keepalive = asyncio.create_task(self._keepalive(ws))
                    try:
                        async for raw in ws:
                            try:
                                if self.on_message(raw):
                                    self.last_data = time.time()
                            except Exception:
                                logger.exception("%s: failed to handle message: %.200s", self.name, raw)
                    finally:
                        keepalive.cancel()
                    detail = "closed"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, handshake failures, ...
                detail = f"{type(e).__name__}: {e}"
            finally:
                self.ws = None
                if self.connected:
                    self.connected = False
                    self.reconnects += 1
                try:
                    self.on_close()
                except Exception:
                    logger.exception("%s: on_close failed", self.name)
            self.on_status(self.name, False, detail or "disconnected")
            if self._should_log():
                logger.warning("%s feed disconnected (%s); reconnecting in %.0fs (#%d)",
                               self.name, detail, backoff, self.reconnects)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)

    async def _keepalive(self, ws) -> None:
        last_ping = time.time()
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            if now - self.last_data > self.idle_timeout:
                if self._should_log():
                    logger.warning("%s: no data for %.0fs; reconnecting", self.name, now - self.last_data)
                await ws.close()
                return
            if self.ping_text is not None and now - last_ping >= self.ping_interval:
                last_ping = now
                try:
                    await ws.send(self.ping_text)
                except Exception:
                    return

    async def send_json(self, obj) -> bool:
        ws = self.ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(obj))
            return True
        except Exception:
            return False


def decode_frame(raw) -> str | None:
    """Text of a data frame, or None for PONG / empty / non-JSON frames."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    raw = raw.strip()
    if not raw or raw.upper() == "PONG" or raw[0] not in "[{":
        return None
    return raw
