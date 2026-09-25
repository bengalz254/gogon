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


# Ways to subscribe, tried in turn until one delivers live updates (not just
# the snapshot). On the server the original spaced filter string gave a
# snapshot and then silence; the docs spell the filter compactly.
VARIANTS = ("compact", "nofilter", "spaced")


def subscribe_message(symbol: str, variant: str = "compact") -> str:
    sub = {"topic": TOPIC, "type": "*"}
    if variant == "compact":
        sub["filters"] = json.dumps({"symbol": symbol}, separators=(",", ":"))
    elif variant == "spaced":
        sub["filters"] = json.dumps({"symbol": symbol})
    return json.dumps({"action": "subscribe", "subscriptions": [sub]})


def parse_message(raw: str, symbol: str, require_tag: bool = False) -> list[tuple[float, float]]:
    """(timestamp_s, price) points from one RTDS message; [] for anything else.

    Handles the snapshot shape ({"payload": {"data": [...]}}) and single
    updates ({"payload": {"symbol", "timestamp", "value"}}). Points tagged
    with a different symbol are ignored; with `require_tag` (unfiltered
    subscription, every coin on one socket) untagged ones are too.
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
    if (tagged and str(tagged).lower() != symbol) or (require_tag and not tagged):
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

    def __init__(self, assets: list[str], symbols: dict[str, str], history, on_tick=None, url: str = RTDS_URL,
                 stale_reconnect_s: float = 10.0):
        self.assets = assets
        self.stale_reconnect_s = stale_reconnect_s
        # asset -> (wall time we got a new price, price); read by the backup feed.
        self.last_seen: dict[str, tuple[float, float]] = {}
        self._max_ts: dict[str, float] = {}
        self.reconnects = 0
        self._live_logged: set[str] = set()
        # Which subscribe variant each coin uses, and whether it has proven to
        # stream live updates (then it's kept for good).
        self.variant: dict[str, int] = {a: 0 for a in assets}
        self.streaming: dict[str, bool] = {a: False for a in assets}
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
                variant = VARIANTS[self.variant[asset] % len(VARIANTS)]
                ws.send(subscribe_message(symbol, variant))
                last_ping = last_new = time.time()
                got_any = False
                updates = 0  # price messages after the first (the snapshot)
                while not self._stop.is_set():
                    now = time.time()
                    if now - last_ping >= 8:
                        ws.send("PING")  # the server drops idle connections
                        last_ping = now
                    if now - last_new > self.stale_reconnect_s:
                        # The socket can stay open yet stop sending prices;
                        # a fresh connection (and snapshot) fixes that.
                        self.reconnects += 1
                        if not self.streaming[asset] and updates == 0:
                            # Snapshot, then nothing: try the next way of subscribing.
                            self.variant[asset] += 1
                        if self.reconnects in (1, 5) or self.reconnects % 50 == 0:
                            logger.warning("[%s] no Chainlink price for %.0fs; reconnecting (#%d, subscribe style '%s')",
                                           asset, now - last_new, self.reconnects, VARIANTS[self.variant[asset] % len(VARIANTS)])
                        break
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    points = parse_message(raw, symbol, require_tag=(variant == "nofilter"))
                    for ts, price in points:
                        if ts > self._max_ts.get(asset, 0.0):
                            self._max_ts[asset] = ts
                            last_new = time.time()
                            self.last_seen[asset] = (last_new, price)
                        if self.history.add(asset, ts, price) and self.on_tick:
                            self.on_tick(asset, ts, price)
                    if points and got_any:
                        updates += 1
                        if updates == 3 and not self.streaming[asset]:
                            self.streaming[asset] = True
                            logger.info("[%s] Chainlink live updates streaming (subscribe style '%s')", asset, variant)
                    if points and not got_any:
                        got_any = True
                        failures = 0
                        if asset not in self._live_logged:  # once; reconnects can be frequent
                            self._live_logged.add(asset)
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
