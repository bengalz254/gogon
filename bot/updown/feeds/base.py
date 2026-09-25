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
    `status(connected, detail)` is reported through `on_status`.
    """

    name = "ws"
    ping_text: str | None = "PING"
    ping_interval: float = 10.0
    idle_timeout: float = 60.0  # force a reconnect if nothing arrives for this long

    def __init__(self, on_status: Callable[[str, bool, str], None] | None = None):
        self.on_status = on_status or (lambda feed, ok, detail: None)
        self.ws = None
        self.connected = False
        self.last_message = 0.0
        self.reconnects = 0

    def url(self) -> str:
        raise NotImplementedError

    async def on_open(self, ws) -> None:
        pass

    def on_message(self, raw) -> None:
        raise NotImplementedError

    def ready(self) -> bool:
        """Whether there is anything to connect for (e.g. tokens to watch)."""
        return True

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
                    self.url(), open_timeout=15, ping_interval=20, ping_timeout=20, max_size=2**24,
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    self.last_message = time.time()
                    await self.on_open(ws)
                    self.on_status(self.name, True, "")
                    logger.info("%s feed connected", self.name)
                    backoff = 1.0
                    keepalive = asyncio.create_task(self._keepalive(ws))
                    try:
                        async for raw in ws:
                            self.last_message = time.time()
                            try:
                                self.on_message(raw)
                            except Exception:
                                logger.exception("%s: failed to handle message: %.200s", self.name, raw)
                    finally:
                        keepalive.cancel()
                    detail = "closed by server"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, handshake failures, ...
                detail = f"{type(e).__name__}: {e}"
            finally:
                self.ws = None
                if self.connected:
                    self.connected = False
                    self.reconnects += 1
            self.on_status(self.name, False, detail or "disconnected")
            logger.warning("%s feed disconnected (%s); reconnecting in %.0fs", self.name, detail, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)

    async def _keepalive(self, ws) -> None:
        last_ping = time.time()
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            if now - self.last_message > self.idle_timeout:
                logger.warning("%s feed idle for %.0fs; forcing reconnect", self.name, self.idle_timeout)
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

