"""Early-warning prices from the venues that move first.

Crypto moves on Binance (and Hyperliquid for HYPE) a second or two before
Chainlink reports it and before Polymarket's books reprice. The market
maker uses these streams only to pull its bids when a sharp move starts;
they never set the strike or the settlement.

Binance: combined aggTrade stream, one socket for all its coins.
Hyperliquid: allMids channel.
Samples are stamped with our own receive time, since what matters is how
early *we* see the move.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from updown.config import BINANCE_SYMBOLS, HYPERLIQUID_COINS

logger = logging.getLogger("polybot.updown.lead")

BINANCE_WS = "wss://stream.binance.com:9443"
HYPERLIQUID_WS = "wss://api.hyperliquid.xyz/ws"
MIN_SAMPLE_GAP_S = 0.1  # store at most ~10 samples per second per coin


def binance_stream_url(assets: list[str], host: str = BINANCE_WS) -> str:
    streams = "/".join(f"{BINANCE_SYMBOLS[a].lower()}@aggTrade" for a in assets)
    return f"{host}/stream?streams={streams}"


def parse_binance(raw: str) -> tuple[str, float] | None:
    """(asset, price) from one combined-stream aggTrade message."""
    try:
        data = json.loads(raw).get("data") or {}
        symbol, price = str(data["s"]).upper(), float(data["p"])
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    for asset, sym in BINANCE_SYMBOLS.items():
        if sym == symbol:
            return (asset, price) if price > 0 else None
    return None


def parse_hyperliquid(raw: str, assets: list[str]) -> list[tuple[str, float]]:
    try:
        msg = json.loads(raw)
        mids = msg["data"]["mids"] if msg.get("channel") == "allMids" else {}
    except (TypeError, ValueError, KeyError, AttributeError):
        return []
    out = []
    for a in assets:
        try:
            p = float(mids[HYPERLIQUID_COINS[a]])
        except (KeyError, TypeError, ValueError):
            continue
        if p > 0:
            out.append((a, p))
    return out


class LeadFeed:
    """Background sockets writing (receive time, price) into `history`."""

    def __init__(self, assets: list[str], history, on_tick=None, binance_ws: str = BINANCE_WS):
        self.history = history
        self.on_tick = on_tick
        self.binance_assets = [a for a in assets if a in BINANCE_SYMBOLS]
        self.hyper_assets = [a for a in assets if a in HYPERLIQUID_COINS]
        self.binance_ws = binance_ws
        self._stop = threading.Event()
        self._last: dict[str, float] = {}
        self._threads = []
        if self.binance_assets:
            self._threads.append(threading.Thread(target=self._run_binance, name="lead-binance", daemon=True))
        if self.hyper_assets:
            self._threads.append(threading.Thread(target=self._run_hyper, name="lead-hyperliquid", daemon=True))

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()

    def _store(self, asset: str, price: float) -> None:
        now = time.time()
        if now - self._last.get(asset, 0.0) < MIN_SAMPLE_GAP_S:
            return
        self._last[asset] = now
        self.history.add(asset, now, price)
        if self.on_tick:
            self.on_tick(asset, now, price)

    def _loop(self, name: str, url: str, on_open, handle, ping_msg: str | None) -> None:
        import websocket  # websocket-client

        failures = 0
        while not self._stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(url, timeout=10)
                ws.settimeout(2)
                if on_open:
                    ws.send(on_open)
                logger.info("%s early-warning feed connected", name)
                last_ping = last_msg = time.time()
                while not self._stop.is_set():
                    now = time.time()
                    if ping_msg and now - last_ping >= 30:
                        ws.send(ping_msg)
                        last_ping = now
                    if now - last_msg > 30:
                        raise TimeoutError("no data for 30s")
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    last_msg = time.time()
                    failures = 0
                    handle(raw)
            except Exception as exc:
                failures += 1
                if failures in (1, 5) or failures % 30 == 0:
                    logger.warning("%s early-warning feed error (%d in a row): %s", name, failures, exc)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            self._stop.wait(min(30, 2 ** min(failures, 5)))

    def _run_binance(self) -> None:
        def handle(raw):
            hit = parse_binance(raw)
            if hit:
                self._store(*hit)

        self._loop("Binance", binance_stream_url(self.binance_assets, self.binance_ws), None, handle, None)

    def _run_hyper(self) -> None:
        def handle(raw):
            for asset, price in parse_hyperliquid(raw, self.hyper_assets):
                self._store(asset, price)

        sub = json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
        self._loop("Hyperliquid", HYPERLIQUID_WS, sub, handle, json.dumps({"method": "ping"}))
