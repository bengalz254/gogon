"""Thin ccxt wrapper for Binance USDⓈ-M futures in hedge mode."""
from __future__ import annotations

import logging

import ccxt

from dca.ladder import LONG

log = logging.getLogger("dcabot")


def open_side(side: str) -> str:
    return "buy" if side == LONG else "sell"


def close_side(side: str) -> str:
    return "sell" if side == LONG else "buy"


def position_side(side: str) -> str:
    return "LONG" if side == LONG else "SHORT"


class BinanceFutures:
    def __init__(self, symbol: str, api_key: str | None = None, secret: str | None = None,
                 demo: bool = True):
        self.symbol = symbol
        self.ex = ccxt.binanceusdm({
            "apiKey": api_key or "",
            "secret": secret or "",
            "enableRateLimit": True,
        })
        if demo and api_key:
            self.ex.enable_demo_trading(True)
        self.ex.load_markets()
        self.market = self.ex.market(symbol)

    # --- market data (no keys needed) -------------------------------------

    def closed_candles(self, timeframe: str, limit: int = 500) -> list[list[float]]:
        """OHLCV rows, oldest first, without the still-forming last candle.

        Binance returns at most 1000 candles per request, so longer histories
        (e.g. for a slow EMA) are paged.
        """
        if limit < 1000:
            return self.ex.fetch_ohlcv(self.symbol, timeframe, limit=limit + 1)[:-1]
        tf_ms = self.ex.parse_timeframe(timeframe) * 1000
        since = self.ex.milliseconds() - (limit + 1) * tf_ms
        by_ts: dict[float, list[float]] = {}
        while True:
            batch = self.ex.fetch_ohlcv(self.symbol, timeframe, since=since, limit=1000)
            for row in batch:
                by_ts[row[0]] = row
            if len(batch) < 1000 or batch[-1][0] + 1 <= since:
                break
            since = batch[-1][0] + 1
        return [by_ts[t] for t in sorted(by_ts)][:-1][-limit:]

    def last_price(self) -> float:
        return float(self.ex.fetch_ticker(self.symbol)["last"])

    # --- account setup ----------------------------------------------------

    def setup_account(self, leverage: int) -> None:
        for what, call in (
            ("hedge position mode", lambda: self.ex.set_position_mode(True, self.symbol)),
            ("isolated margin", lambda: self.ex.set_margin_mode("isolated", self.symbol)),
            (f"leverage {leverage}x", lambda: self.ex.set_leverage(leverage, self.symbol)),
        ):
            try:
                call()
                log.info("Binance: set %s", what)
            except ccxt.ExchangeError as e:
                if "No need to change" in str(e):
                    log.info("Binance: %s already set", what)
                else:
                    raise

    def free_usdt(self) -> float:
        return float(self.ex.fetch_balance()["USDT"]["free"] or 0.0)

    def position_qty(self, side: str) -> float:
        for p in self.ex.fetch_positions([self.symbol]):
            if p.get("side") == side:
                return abs(float(p.get("contracts") or 0.0))
        return 0.0

    # --- precision --------------------------------------------------------

    def amount(self, qty: float) -> float:
        return float(self.ex.amount_to_precision(self.symbol, qty))

    def price(self, price: float) -> float:
        return float(self.ex.price_to_precision(self.symbol, price))

    def min_notional(self) -> float:
        return float(((self.market.get("limits") or {}).get("cost") or {}).get("min") or 5.0)

    # --- orders -----------------------------------------------------------

    def market_open(self, side: str, qty: float) -> dict:
        return self.ex.create_order(
            self.symbol, "market", open_side(side), qty, None,
            {"positionSide": position_side(side), "newOrderRespType": "RESULT"},
        )

    def market_close(self, side: str, qty: float) -> dict:
        return self.ex.create_order(
            self.symbol, "market", close_side(side), qty, None,
            {"positionSide": position_side(side), "newOrderRespType": "RESULT"},
        )

    def limit_open(self, side: str, qty: float, price: float) -> dict:
        return self.ex.create_order(
            self.symbol, "limit", open_side(side), qty, price,
            {"positionSide": position_side(side), "timeInForce": "GTC"},
        )

    def limit_close(self, side: str, qty: float, price: float) -> dict:
        return self.ex.create_order(
            self.symbol, "limit", close_side(side), qty, price,
            {"positionSide": position_side(side), "timeInForce": "GTC"},
        )

    def stop_close(self, side: str, qty: float, stop_price: float) -> dict:
        """STOP_MARKET on Binance's side, so SL still fires if the bot is offline."""
        return self.ex.create_order(
            self.symbol, "market", close_side(side), qty, None,
            {"positionSide": position_side(side), "stopLossPrice": stop_price},
        )

    def fetch_order(self, order_id: str, trigger: bool = False) -> dict:
        return self.ex.fetch_order(order_id, self.symbol, {"trigger": True} if trigger else {})

    def cancel(self, order_id: str | None, trigger: bool = False) -> None:
        if not order_id:
            return
        try:
            self.ex.cancel_order(order_id, self.symbol, {"trigger": True} if trigger else {})
        except ccxt.OrderNotFound:
            pass  # already filled or cancelled
