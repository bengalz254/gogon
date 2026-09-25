"""Direct exchange trade feeds (Binance aggTrade / Bybit publicTrade) used
only as a fast leading indicator, plus a 1-minute kline bootstrap so the
1h regime filter and volatility estimates work right after startup.

Settlement never uses these prices -- only the Chainlink oracle does.
"""
from __future__ import annotations

import json
import logging
import time

import requests

from bot.updown.events import CexTick
from bot.updown.feeds.base import WsFeed

logger = logging.getLogger("polybot.updown.feeds.cex")


def parse_binance(obj, symbol_map: dict) -> list:
    data = obj.get("data", obj) if isinstance(obj, dict) else None
    if not isinstance(data, dict) or data.get("e") not in ("aggTrade", "trade"):
        return []
    asset = symbol_map.get(str(data.get("s", "")).lower())
    try:
        price, ts = float(data["p"]), float(data["T"]) / 1000.0
    except (KeyError, TypeError, ValueError):
        return []
    return [CexTick(asset, ts, price)] if asset else []


def parse_bybit(obj, symbol_map: dict) -> list:
    if not isinstance(obj, dict) or not str(obj.get("topic", "")).startswith("publicTrade."):
        return []
    out = []
    for t in obj.get("data") or []:
        asset = symbol_map.get(str(t.get("s", "")).lower())
        try:
            price, ts = float(t["p"]), float(t["T"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        if asset:
            out.append(CexTick(asset, ts, price))
    return out


class _ThrottledCex(WsFeed):
    name = "cex"
    idle_timeout = 30.0

    def __init__(self, symbol_map: dict, emit, throttle_ms: float, on_status=None):
        super().__init__(on_status)
        self.symbol_map = symbol_map  # "btcusdt" -> "btc"
        self.emit = emit
        self.throttle_s = throttle_ms / 1000.0
        self._last_emit: dict = {}

    def _emit_throttled(self, events) -> None:
        for ev in events:
            last = self._last_emit.get(ev.asset, 0.0)
            if ev.ts - last >= self.throttle_s:
                self._last_emit[ev.asset] = ev.ts
                self.emit(ev)


class BinanceFeed(_ThrottledCex):
    ping_text = None  # Binance pings us; the websockets library answers automatically

    def __init__(self, base_url: str, symbol_map: dict, emit, throttle_ms: float = 100.0, on_status=None):
        super().__init__(symbol_map, emit, throttle_ms, on_status)
        self.base_url = base_url

    def url(self) -> str:
        streams = "/".join(f"{s}@aggTrade" for s in sorted(self.symbol_map))
        return f"{self.base_url}?streams={streams}"

    def on_message(self, raw) -> None:
        self._emit_throttled(parse_binance(json.loads(raw), self.symbol_map))


class BybitFeed(_ThrottledCex):
    ping_text = json.dumps({"op": "ping"})
    ping_interval = 20.0

    def __init__(self, url: str, symbol_map: dict, emit, throttle_ms: float = 100.0, on_status=None):
        super().__init__(symbol_map, emit, throttle_ms, on_status)
        self._url = url

    def url(self) -> str:
        return self._url

    async def on_open(self, ws) -> None:
        args = [f"publicTrade.{s.upper()}" for s in sorted(self.symbol_map)]
        await ws.send(json.dumps({"op": "subscribe", "args": args}))

    def on_message(self, raw) -> None:
        self._emit_throttled(parse_bybit(json.loads(raw), self.symbol_map))


def bootstrap_klines(symbol_map: dict, binance_rest: str, bybit_rest: str, minutes: int = 70,
                     timeout: float = 6.0) -> list:
    """~1h of 1-minute closes per asset as CexTick events (oldest first).
    Tries Binance, then Bybit; returns [] if both are unreachable."""
    out = []
    for sym, asset in symbol_map.items():
        ticks = []
        try:
            r = requests.get(
                f"{binance_rest.rstrip('/')}/api/v3/klines",
                params={"symbol": sym.upper(), "interval": "1m", "limit": minutes}, timeout=timeout,
            )
            r.raise_for_status()
            now = time.time()
            for k in r.json():
                close_ts = float(k[6]) / 1000.0
                if close_ts <= now:
                    ticks.append(CexTick(asset, close_ts, float(k[4])))
        except Exception as e:
            logger.info("Binance klines unavailable for %s (%s); trying Bybit", sym, e)
            try:
                r = requests.get(
                    f"{bybit_rest.rstrip('/')}/v5/market/kline",
                    params={"category": "spot", "symbol": sym.upper(), "interval": "1", "limit": minutes},
                    timeout=timeout,
                )
                r.raise_for_status()
                now = time.time()
                for k in reversed(r.json().get("result", {}).get("list", [])):
                    close_ts = float(k[0]) / 1000.0 + 60.0
                    if close_ts <= now:
                        ticks.append(CexTick(asset, close_ts, float(k[4])))
            except Exception as e2:
                logger.warning("No kline bootstrap for %s: %s", sym, e2)
        out.extend(ticks)
    out.sort(key=lambda e: e.ts)
    return out
