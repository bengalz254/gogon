"""Migration events from PumpPortal's public WebSocket (subscribeMigration).

PumpPortal is a third-party API for pump.fun, not run by pump.fun. Its
payloads are not a versioned contract, so parsing is defensive: anything
with a valid `mint` after subscribing to migrations counts as a migration.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time

from migbot.http import redact
from migbot.models import MigrationEvent
from migbot.parsing import ms_or_s_to_ts, num, text
from migbot.solana import is_pubkey

logger = logging.getLogger("migbot.pumpportal")


def parse_migration(payload, received_at: float) -> MigrationEvent | None:
    if not isinstance(payload, dict):
        return None
    mint = text(payload, "mint", "mintAddress", "token")
    if not is_pubkey(mint):
        return None  # subscription confirmations, errors, other messages
    # Only migrations are subscribed; skip trade/creation events if any leak through.
    if text(payload, "txType", "type").lower() in ("buy", "sell", "create"):
        return None
    ts = ms_or_s_to_ts(num(payload, "timestamp", "blockTime", "time"))
    # Trust an event timestamp only when it is close to our clock.
    migrated_at = ts if ts and abs(ts - received_at) < 600 else received_at
    return MigrationEvent(
        mint=mint,
        source="pumpportal",
        migrated_at=migrated_at,
        received_at=received_at,
        pool=text(payload, "pool", "poolType"),
        signature=text(payload, "signature", "txSignature"),
        symbol=text(payload, "symbol"),
        name=text(payload, "name"),
    )


class PumpPortalFeed:
    """Background WebSocket listener; migration events go onto `events`."""

    def __init__(self, ws_url: str, api_key: str = "", reconnect_after_silence_minutes: float = 30.0):
        self.ws_url = ws_url
        self.api_key = api_key
        self.silence_limit = reconnect_after_silence_minutes * 60
        self.events: "queue.Queue[MigrationEvent]" = queue.Queue()
        self.connected = False
        self.connected_since = 0.0
        self.last_message_at = 0.0
        self.last_event_at = 0.0
        self.event_count = 0
        self.reconnects = 0
        self.last_error = ""
        self._ws = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _url(self) -> str:
        if not self.api_key:
            return self.ws_url
        sep = "&" if "?" in self.ws_url else "?"
        return f"{self.ws_url}{sep}api-key={self.api_key}"

    def handle_message(self, message: str) -> MigrationEvent | None:
        now = time.time()
        self.last_message_at = now
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            return None
        event = parse_migration(payload, now)
        if event is not None:
            self.last_event_at = now
            self.event_count += 1
            self.events.put(event)
        elif isinstance(payload, dict) and payload.get("errors"):
            self.last_error = redact(payload.get("errors"))[:200]
            logger.warning("PumpPortal error message: %s", self.last_error)
        return event

    def start(self) -> None:
        import websocket  # websocket-client

        def on_open(ws):
            self.connected = True
            self.connected_since = time.time()
            self.last_message_at = time.time()
            logger.info("PumpPortal connected, subscribing to migrations")
            ws.send(json.dumps({"method": "subscribeMigration"}))

        def on_message(ws, message):
            self.handle_message(message)

        def on_error(ws, error):
            self.last_error = redact(error)[:200]
            logger.warning("PumpPortal socket error: %s", self.last_error)

        def on_close(ws, status, msg):
            self.connected = False
            logger.warning("PumpPortal socket closed (status=%s %s)", status, msg or "")

        def watchdog():
            while not self._stop.wait(30):
                ws = self._ws
                if ws is not None and self.connected and time.time() - self.last_message_at > self.silence_limit:
                    logger.warning("PumpPortal silent for %.0f min, reconnecting", self.silence_limit / 60)
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001 - best effort
                        pass

        def run():
            backoff = 2.0
            while not self._stop.is_set():
                started = time.time()
                try:
                    self._ws = websocket.WebSocketApp(
                        self._url(), on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close
                    )
                    self._ws.run_forever(ping_interval=30, ping_timeout=10)
                except Exception as exc:  # noqa: BLE001 - keep the feed alive
                    self.last_error = redact(exc)[:200]
                    logger.exception("PumpPortal feed crashed")
                self.connected = False
                if self._stop.is_set():
                    break
                if time.time() - started > 120:
                    backoff = 2.0
                self.reconnects += 1
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)

        self._thread = threading.Thread(target=run, name="pumpportal", daemon=True)
        self._thread.start()
        threading.Thread(target=watchdog, name="pumpportal-watchdog", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def drain(self) -> list[MigrationEvent]:
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    def snapshot(self) -> dict:
        return {
            "name": "PumpPortal",
            "connected": self.connected,
            "connected_since": self.connected_since,
            "last_message_at": self.last_message_at,
            "last_event_at": self.last_event_at,
            "event_count": self.event_count,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }
