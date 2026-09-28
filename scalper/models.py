"""Plain data types shared by the strategy, risk, backtest and live code.

Prices and quantities are floats everywhere in the decision logic and are
only converted to exact Decimal strings (snapped to the exchange's tick and
step sizes) at the edge, right before an order is sent.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

LONG = "LONG"
SHORT = "SHORT"

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError:
        raise ValueError(
            f"Unsupported interval {interval!r}; use one of {', '.join(INTERVAL_MS)}"
        ) from None


def opposite(side: str) -> str:
    return SHORT if side == LONG else LONG


def order_side(side: str) -> str:
    """Binance order side that OPENS a position in this direction."""
    return "BUY" if side == LONG else "SELL"


def close_side(side: str) -> str:
    """Binance order side that CLOSES a position in this direction."""
    return "SELL" if side == LONG else "BUY"


@dataclass
class Candle:
    open_time: int  # ms since epoch
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    close_time: int = 0  # ms, Binance style: open_time + interval - 1

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass
class Signal:
    symbol: str
    side: str  # LONG / SHORT
    strategy: str
    entry_ref: float  # price the levels were computed from (signal candle close)
    stop: float
    take_profit: float
    atr: float
    time: int  # close time (ms) of the candle that produced the signal
    reason: str
    # When set, the take-profit is re-anchored to the real fill price as
    # entry ± tp_r × (entry − stop). When None, take_profit is a fixed level
    # (e.g. mean-reversion targets the Bollinger mid-band).
    tp_r: float | None = None

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_ref - self.stop)


def _to_dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def snap(value, step, mode: str = "floor") -> Decimal:
    """Snap `value` onto a multiple of `step`.

    mode: "floor" (toward -inf), "ceil" (toward +inf) or "nearest".
    A tiny epsilon absorbs float noise such as 0.30000000000000004 or
    0.0029999999 so exact multiples don't get pushed down a whole step.
    """
    v = _to_dec(value)
    s = _to_dec(step)
    if s <= 0:
        return v
    units = v / s
    eps = Decimal("1e-9")
    if mode == "floor":
        n = (units + eps).to_integral_value(rounding=ROUND_FLOOR)
    elif mode == "ceil":
        n = (units - eps).to_integral_value(rounding=ROUND_CEILING)
    elif mode == "nearest":
        n = units.to_integral_value(rounding=ROUND_HALF_UP)
    else:
        raise ValueError(f"unknown snap mode {mode!r}")
    return n * s


def fmt_decimal(d: Decimal) -> str:
    """Plain decimal string without exponent or trailing zeros ("100", "0.001")."""
    s = format(d.normalize(), "f")
    return s if s not in ("-0", "") else "0"


@dataclass
class SymbolRules:
    """Exchange trading rules for one symbol (from /fapi/v1/exchangeInfo)."""

    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    margin_asset: str = "USDT"

    def qty(self, qty: float, market: bool = True) -> Decimal:
        """Round a quantity DOWN to a valid lot (never exceeds the risk budget)."""
        step = self.market_step_size if market else self.step_size
        max_q = self.market_max_qty if market else self.max_qty
        q = snap(qty, step, "floor")
        if max_q > 0 and q > max_q:
            q = snap(max_q, step, "floor")
        return q

    def min_qty_for(self, market: bool = True) -> Decimal:
        return self.market_min_qty if market else self.min_qty

    def price(self, price: float, mode: str = "nearest") -> Decimal:
        return snap(price, self.tick_size, mode)

    def stop_price(self, price: float, side: str) -> Decimal:
        """Round a stop-loss level AWAY from the market.

        A long's stop sits below price, so it is floored; a short's stop is
        ceiled. This keeps the stop at least as far away as the strategy
        intended — never accidentally inside the noise it was placed to
        avoid.
        """
        return self.price(price, "floor" if side == LONG else "ceil")

    def target_price(self, price: float, side: str) -> Decimal:
        """Round a take-profit level TOWARD the entry so it is never harder to hit."""
        return self.price(price, "floor" if side == LONG else "ceil")

    def check_order(self, qty: Decimal, price: float, market: bool = True) -> str:
        """Return "" if the order satisfies the lot / notional filters, else a reason."""
        min_q = self.min_qty_for(market)
        if qty <= 0 or qty < min_q:
            return f"qty {fmt_decimal(qty)} below exchange minimum {fmt_decimal(min_q)}"
        notional = qty * _to_dec(price)
        if self.min_notional > 0 and notional < self.min_notional:
            return (
                f"notional {float(notional):.2f} below exchange minimum "
                f"{fmt_decimal(self.min_notional)} {self.margin_asset}"
            )
        return ""


@dataclass
class Fill:
    qty: float
    avg_price: float
    fee: float = 0.0  # in the margin asset
    order_id: str = ""
    client_id: str = ""


@dataclass
class PositionInfo:
    symbol: str
    qty: float  # signed: > 0 long, < 0 short
    entry_price: float
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0
    liquidation_price: float = 0.0

    @property
    def side(self) -> str:
        return LONG if self.qty > 0 else SHORT

    @property
    def is_open(self) -> bool:
        return abs(self.qty) > 0


@dataclass
class OrderRef:
    """Handle to an order resting on the exchange (regular or algo/conditional)."""

    kind: str  # "regular" | "algo" | "paper"
    order_id: str
    client_id: str
    purpose: str = ""  # "sl" | "tp" | "entry" | ""
    order_type: str = ""
    price: float = 0.0  # trigger price for stops, limit price for TPs

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "order_id": self.order_id,
            "client_id": self.client_id,
            "purpose": self.purpose,
            "order_type": self.order_type,
            "price": self.price,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "OrderRef | None":
        if not d:
            return None
        return cls(
            kind=d.get("kind", "regular"),
            order_id=str(d.get("order_id", "")),
            client_id=str(d.get("client_id", "")),
            purpose=d.get("purpose", ""),
            order_type=d.get("order_type", ""),
            price=float(d.get("price", 0.0) or 0.0),
        )


@dataclass
class ClosedTradeInfo:
    """What the broker reports about how a position was closed."""

    exit_price: float
    qty: float
    gross_pnl: float  # before fees
    fees: float  # entry + exit commissions, in the margin asset
    funding: float = 0.0  # funding paid (+) or received (-)
    exit_time: int = 0
    approximate: bool = False
