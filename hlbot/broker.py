"""Order execution: a paper (simulated) broker and a live Hyperliquid broker.

Both expose the same small interface used by `hlbot.main.LiveBot`:
    setup(), mid_price(), get_position(), open(), close(),
    sync_stop(), cancel_stop()
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Optional

from hlbot.strategy import LONG, SHORT

logger = logging.getLogger("hlbot.broker")


@dataclass
class PositionInfo:
    side: str  # LONG | SHORT
    size: float  # in coin units, always positive
    entry_price: float


@dataclass
class Fill:
    price: float
    size: float


class OrderError(RuntimeError):
    pass


class PaperBroker:
    """Simulates fills at the current mid price (+ slippage). No funds at risk.
    Position state lives in memory and is persisted by LiveBot via to_dict()."""

    def __init__(self, info, coin: str, taker_fee: float = 0.00045, slippage: float = 0.0002):
        self.info = info
        self.coin = coin
        self.taker_fee = taker_fee
        self.slippage = slippage
        self.position: Optional[PositionInfo] = None
        self.realized_pnl = 0.0

    def setup(self, leverage: int, is_cross: bool) -> None:
        logger.info("[PAPER] leverage %dx %s on %s", leverage, "cross" if is_cross else "isolated", self.coin)

    def mid_price(self) -> float:
        return self.info.mid_price(self.coin)

    def get_position(self) -> Optional[PositionInfo]:
        return self.position

    def open(self, side: str, notional_usd: float) -> Fill:
        if self.position is not None:
            raise OrderError("paper position already open")
        d = 1 if side == LONG else -1
        price = self.mid_price() * (1 + d * self.slippage)
        size = notional_usd / price
        self.realized_pnl -= notional_usd * self.taker_fee
        self.position = PositionInfo(side, size, price)
        return Fill(price, size)

    def close(self) -> Optional[Fill]:
        pos = self.position
        if pos is None:
            return None
        d = 1 if pos.side == LONG else -1
        price = self.mid_price() * (1 - d * self.slippage)
        self.realized_pnl += d * (price - pos.entry_price) * pos.size - price * pos.size * self.taker_fee
        self.position = None
        return Fill(price, pos.size)

    def sync_stop(self, side: str, size: float, stop_price: float) -> None:
        pass  # the paper bot checks stops itself

    def cancel_stop(self) -> None:
        pass

    def to_dict(self) -> dict:
        pos = self.position
        return {
            "realized_pnl": self.realized_pnl,
            "position": None if pos is None else {"side": pos.side, "size": pos.size, "entry_price": pos.entry_price},
        }

    def load_dict(self, data: dict) -> None:
        self.realized_pnl = float(data.get("realized_pnl", 0.0))
        pos = data.get("position")
        self.position = PositionInfo(pos["side"], float(pos["size"]), float(pos["entry_price"])) if pos else None


def round_price(px: float, sz_decimals: int) -> float:
    """Hyperliquid perp prices: max 5 significant figures and max (6 - szDecimals) decimals."""
    return round(float(f"{px:.5g}"), max(0, 6 - sz_decimals))


def round_size(sz: float, sz_decimals: int) -> float:
    return round(sz, sz_decimals)


def _parse_order_response(resp) -> Optional[Fill]:
    """Return the fill from an SDK order response, None if nothing filled,
    raise OrderError if the exchange rejected the order."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        raise OrderError(f"order rejected: {resp}")
    statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
    for st in statuses:
        if "error" in st:
            raise OrderError(st["error"])
        if "filled" in st:
            f = st["filled"]
            return Fill(float(f["avgPx"]), float(f["totalSz"]))
    return None


class HyperliquidBroker:
    """Real orders on Hyperliquid via the official hyperliquid-python-sdk.

    Entries/exits are IOC "market" orders (aggressive limit with
    `max_slippage`). Once the trailing stop is armed a reduce-only stop-market
    trigger order is kept on the exchange as a safety net, so the position is
    still protected if the bot goes offline.
    """

    def __init__(
        self,
        coin: str,
        secret_key: str,
        account_address: str,
        base_url: str,
        max_slippage: float = 0.01,
        stop_order_min_move: float = 0.001,
    ):
        import eth_account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info

        self.coin = coin
        self.address = account_address
        self.max_slippage = max_slippage
        self.stop_order_min_move = stop_order_min_move
        wallet = eth_account.Account.from_key(secret_key)
        self.info = Info(base_url, skip_ws=True)
        self.exchange = Exchange(wallet, base_url, account_address=account_address)
        meta = self.info.meta()
        asset = next((a for a in meta["universe"] if a["name"] == coin), None)
        if asset is None:
            raise ValueError(f"Coin {coin!r} is not listed on Hyperliquid perps")
        self.sz_decimals = int(asset["szDecimals"])
        self.max_leverage = int(asset.get("maxLeverage", 0)) or None
        self.stop_cloid: Optional[str] = None
        self.stop_price: Optional[float] = None

    def setup(self, leverage: int, is_cross: bool) -> None:
        if self.max_leverage and leverage > self.max_leverage:
            raise ValueError(f"{self.coin} max leverage is {self.max_leverage}x, config asks for {leverage}x")
        resp = self.exchange.update_leverage(leverage, self.coin, is_cross=is_cross)
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise OrderError(f"update_leverage failed: {resp}")
        logger.info("Leverage set to %dx (%s) on %s", leverage, "cross" if is_cross else "isolated", self.coin)

    def mid_price(self) -> float:
        return float(self.info.all_mids()[self.coin])

    def get_position(self) -> Optional[PositionInfo]:
        state = self.info.user_state(self.address)
        for ap in state.get("assetPositions", []):
            p = ap.get("position", {})
            if p.get("coin") != self.coin:
                continue
            szi = float(p.get("szi", 0) or 0)
            if szi == 0:
                return None
            return PositionInfo(LONG if szi > 0 else SHORT, abs(szi), float(p.get("entryPx") or 0))
        return None

    def open(self, side: str, notional_usd: float) -> Fill:
        size = round_size(notional_usd / self.mid_price(), self.sz_decimals)
        if size <= 0:
            raise OrderError(f"order size rounds to 0 for ${notional_usd:.2f} notional")
        resp = self.exchange.market_open(self.coin, side == LONG, size, None, self.max_slippage)
        fill = _parse_order_response(resp)
        if fill is None:
            raise OrderError(f"entry order did not fill: {resp}")
        return fill

    def close(self) -> Optional[Fill]:
        if self.get_position() is None:
            self.cancel_stop()
            return None
        resp = self.exchange.market_close(self.coin, slippage=self.max_slippage)
        fill = _parse_order_response(resp)
        if fill is None:
            raise OrderError(f"close order did not fill: {resp}")
        self.cancel_stop()  # only after the position is gone, so it is never unprotected
        return fill

    def sync_stop(self, side: str, size: float, stop_price: float) -> None:
        """Place or move the exchange-side reduce-only stop-market order.
        The new order is placed before the old one is cancelled, so the
        position is never left without a stop."""
        from hyperliquid.utils.types import Cloid

        if self.stop_price is not None and abs(stop_price - self.stop_price) / self.stop_price < self.stop_order_min_move:
            return
        is_buy = side == SHORT  # closing a short = buy
        trigger = round_price(stop_price, self.sz_decimals)
        limit = round_price(stop_price * (1 + self.max_slippage if is_buy else 1 - self.max_slippage), self.sz_decimals)
        order_type = {"trigger": {"triggerPx": trigger, "isMarket": True, "tpsl": "sl"}}
        cloid = Cloid.from_int(secrets.randbits(128))
        try:
            resp = self.exchange.order(
                self.coin, is_buy, round_size(size, self.sz_decimals), limit, order_type, reduce_only=True, cloid=cloid
            )
            _parse_order_response(resp)
        except Exception as e:
            logger.warning("Exchange stop order at %s failed: %s", trigger, e)
            return
        old = self.stop_cloid
        self.stop_cloid = str(cloid)
        self.stop_price = stop_price
        logger.info("Exchange stop order placed at %s", trigger)
        if old:
            self._cancel_cloid(old)

    def cancel_stop(self) -> None:
        if self.stop_cloid:
            self._cancel_cloid(self.stop_cloid)
        self.stop_cloid = None
        self.stop_price = None

    def _cancel_cloid(self, cloid: str) -> None:
        from hyperliquid.utils.types import Cloid

        try:
            self.exchange.cancel_by_cloid(self.coin, Cloid.from_str(cloid))
        except Exception:
            logger.exception("Failed to cancel stop order %s (it may already be filled/cancelled)", cloid)

    def to_dict(self) -> dict:
        return {"stop_cloid": self.stop_cloid, "stop_price": self.stop_price}

    def load_dict(self, data: dict) -> None:
        self.stop_cloid = data.get("stop_cloid")
        self.stop_price = data.get("stop_price")
