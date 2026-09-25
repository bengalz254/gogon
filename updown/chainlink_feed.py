"""Chainlink prices from Polymarket's real-time data socket (RTDS).

The 5-minute markets settle on Chainlink (a 60s TWAP of it since Aug 2026).
Binance is a proxy with its own small, shifting gap to Chainlink; this feed
removes that gap by reading Chainlink's per-second prices directly, relayed
for free by Polymarket at wss://ws-live-data.polymarket.com.

Subscribe format confirmed with scripts/probe_rtds.py:
    {"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink",
     "type": "*", "filters": "{\\"symbol\\": \\"btc/usd\\"}"}]}
The first reply is a snapshot ({"payload": {"data": [{timestamp, value}, ...]}})
with the last couple of minutes, which fills the opening TWAP right away;
live updates follow. One connection per coin keeps symbols unambiguous.
Samples are stored at Chainlink's observation time (ms -> s), so our TWAPs
line up with the oracle's own seconds.
"""
from __future__ import annotations

import json
import logging
import threading
import time

logger = logging.getLogger("polybot.updown.chainlink")

RTDS_URL = "wss://ws-live-data.polymarket.com"
TOPIC = "crypto_prices_chainlink"


def subscribe_message(symbol: str) -> str:
    return json.dumps({
        "action": "subscribe",
        "subscriptions": [{"topic": TOPIC, "type": "*", "filters": json.dumps({"symbol": symbol})}],
    })


def parse_message(raw: str, symbol: str) -> list[tuple[float, float]]:
    """(timestamp_s, price) points from one RTDS message; [] for anything else.

    Handles the snapshot shape ({"payload": {"data": [...]}}) and single
    updates ({"payload": {"symbol", "timestamp", "value"}}). Points tagged
    with a different symbol are ignored.
    """
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(msg, dict):
        return []
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return []
    tagged = payload.get("symbol")
    if tagged and str(tagged).lower() != symbol:
        return []
    rows = payload.get("data") if isinstance(payload.get("data"), list) else [payload]
    out = []
    for r in rows:
        try:
            ts, value = float(r["timestamp"]), float(r["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts > 1e12:  # milliseconds
            ts /= 1000.0
        if value > 0:
            out.append((ts, value))
    return out


class ChainlinkRTDSFeed:
    """One background thread + websocket per coin; reconnects on its own."""

    name = "Chainlink RTDS"

    def __init__(self, assets: list[str], symbols: dict[str, str], history, on_tick=None, url: str = RTDS_URL):
        self.assets = assets
        self.symbols = symbols  # asset -> "btc/usd"
        self.history = history
        self.on_tick = on_tick
        self.url = url
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._run, args=(a,), name=f"rtds-{a}", daemon=True) for a in assets]
        self._sockets: dict[str, object] = {}

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for ws in list(self._sockets.values()):
            try:
                ws.close()
            except Exception:
                pass

    def seed_sigma(self, asset: str, minutes: int = 120) -> float | None:
        return None  # no history endpoint here; the engine seeds from Binance candles instead

    def _run(self, asset: str) -> None:
        import websocket  # websocket-client

        symbol = self.symbols[asset]
        failures = 0
        while not self._stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(self.url, timeout=10)
                ws.settimeout(2)  # short reads, so the keep-alive PING goes out on time
                self._sockets[asset] = ws
                ws.send(subscribe_message(symbol))
                last_ping = time.time()
                got_any = False
                while not self._stop.is_set():
                    if time.time() - last_ping >= 8:
                        ws.send("PING")  # the server drops idle connections
                        last_ping = time.time()
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    points = parse_message(raw, symbol)
                    for ts, price in points:
                        self.history.add(asset, ts, price)
                        if self.on_tick:
                            self.on_tick(asset, ts, price)
                    if points and not got_any:
                        got_any = True
                        failures = 0
                        logger.info("[%s] Chainlink feed live (%s)", asset, symbol)
                    elif not points and raw not in ("PONG", "") and "statusCode" in str(raw):
                        logger.warning("[%s] RTDS said: %s", asset, str(raw)[:200])
            except Exception as exc:
                failures += 1
                if failures in (1, 5) or failures % 30 == 0:
                    logger.warning("[%s] Chainlink feed error (%d in a row): %s", asset, failures, exc)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            self._stop.wait(min(30, 2 ** min(failures, 5)))
