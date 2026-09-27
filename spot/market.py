"""Tokocrypto market data through ccxt: public endpoints only, no API key.

Tokocrypto serves some pairs from its own order book ("native" pairs) and
the rest from Binance's, whose data then comes from api.binance.com. ccxt
picks the right host for each pair; this module only uses calls that work
for both kinds: the pair list, the order book and 1-minute candles.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("spot.market")

CANDLE_MS = 60_000
# Candles per request, and requests per catch-up (30 x 500 min ≈ 10 days).
CANDLE_PAGE = 500
MAX_CANDLE_PAGES = 30
# A minute's candle is treated as final this long after the minute ends.
CLOSE_GRACE_MS = 2_000


class DataError(Exception):
    """Market data couldn't be fetched (network, exchange or location problem)."""


class SymbolError(Exception):
    """The configured pair doesn't exist or isn't trading; retrying won't help."""


@dataclass(frozen=True)
class MarketRules:
    symbol: str
    base: str
    quote: str
    tick: float  # price step
    step: float  # amount step
    min_amount: float
    min_cost: float
    native: bool  # served by Tokocrypto's own endpoints (else Binance-backed)


@dataclass(frozen=True)
class Candle:
    ts: int  # opening time of the minute, in ms
    open: float
    high: float
    low: float
    close: float


def _num(value) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def parse_rules(market: dict, native: bool) -> MarketRules:
    """MarketRules from a ccxt market (ccxt reports Tokocrypto's precision as tick sizes)."""
    precision = market.get("precision") or {}
    limits = market.get("limits") or {}
    tick = _num(precision.get("price"))
    step = _num(precision.get("amount"))
    if tick <= 0 or step <= 0:
        raise SymbolError(f"Tokocrypto tidak memberi ukuran tick/lot untuk {market.get('symbol')}")
    return MarketRules(
        symbol=market["symbol"],
        base=market["base"],
        quote=market["quote"],
        tick=tick,
        step=step,
        min_amount=_num((limits.get("amount") or {}).get("min")),
        min_cost=_num((limits.get("cost") or {}).get("min")),
        native=native,
    )


def describe_error(exc: Exception) -> str:
    """A one-line reason for a failed request, with a hint for location blocks."""
    text = " ".join(str(exc).split())[:200]
    reason = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    if any(marker in text for marker in ("451", "403", "restricted location", "Forbidden")):
        reason += " — kemungkinan lokasi server (VPS) diblokir; cek dengan scripts/spot_check.py"
    return reason


class TokocryptoData:
    def __init__(self, symbol: str, client=None):
        if client is None:
            import ccxt

            client = ccxt.tokocrypto({"enableRateLimit": True, "timeout": 15_000})
        self.client = client
        self.symbol = symbol

    def _call(self, what: str, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:
            raise DataError(f"Gagal {what}: {describe_error(exc)}") from exc

    def _native(self, market: dict) -> bool:
        is_native = getattr(self.client, "is_native_market", None)
        return bool(is_native(market)) if callable(is_native) else False

    def rules(self) -> MarketRules:
        markets = self._call("memuat daftar pasangan Tokocrypto", self.client.load_markets)
        market = markets.get(self.symbol)
        if market is None:
            base = self.symbol.split("/")[0]
            similar = sorted(s for s in markets if s.split("/")[0] == base)[:10]
            hint = f" Pasangan {base} yang ada: {', '.join(similar)}." if similar else ""
            raise SymbolError(
                f"Pasangan {self.symbol} tidak ada di Tokocrypto.{hint} "
                "Lihat semua pasangan: python scripts/spot_check.py --symbols IDR"
            )
        if market.get("active") is False:
            raise SymbolError(f"Pasangan {self.symbol} sedang tidak aktif untuk trading spot di Tokocrypto.")
        return parse_rules(market, self._native(market))

    def symbols(self, quote: str | None = None) -> list[tuple[str, bool]]:
        """Active spot pairs as (symbol, native), optionally only one quote currency."""
        markets = self._call("memuat daftar pasangan Tokocrypto", self.client.load_markets)
        result = []
        for symbol, market in sorted(markets.items()):
            if market.get("active") is False or market.get("spot") is False:
                continue
            if quote and market.get("quote") != quote:
                continue
            result.append((symbol, self._native(market)))
        return result

    def top_of_book(self) -> tuple[float | None, float | None]:
        """Best bid and best ask (None for an empty side)."""
        book = self._call("mengambil order book", self.client.fetch_order_book, self.symbol, 5)
        bids, asks = book.get("bids") or [], book.get("asks") or []
        return (float(bids[0][0]) if bids else None, float(asks[0][0]) if asks else None)

    def closed_candles(self, since_ms: int, now_ms: int) -> list[Candle]:
        """1-minute candles opened at or after `since_ms` that had closed by
        `now_ms`, oldest first. A long gap is fetched page by page; what
        doesn't fit in MAX_CANDLE_PAGES is picked up by the next call."""
        candles: list[Candle] = []
        cursor = since_ms
        for _ in range(MAX_CANDLE_PAGES):
            rows = self._call("mengambil candle", self.client.fetch_ohlcv, self.symbol, "1m", cursor, CANDLE_PAGE) or []
            last_ts = None
            for row in sorted(rows, key=lambda r: r[0]):
                ts = int(row[0])
                if ts < cursor:
                    continue
                if ts + CANDLE_MS + CLOSE_GRACE_MS > now_ms:
                    return candles  # this one and the rest are still forming
                candles.append(Candle(ts, float(row[1]), float(row[2]), float(row[3]), float(row[4])))
                last_ts = ts
            if last_ts is None or len(rows) < CANDLE_PAGE:
                break
            cursor = last_ts + CANDLE_MS
        return candles
