"""Deal state: one DCA cycle (base order + safety orders) on one side."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from dca.ladder import average_price, pnl, take_profit_price


@dataclass
class Order:
    index: int  # 0 = base order
    price: float  # planned limit price (base: actual entry)
    qty: float
    filled_qty: float = 0.0
    fill_price: float = 0.0
    order_id: str | None = None


@dataclass
class Deal:
    deal_id: str
    side: str
    entry_price: float
    take_profit_pct: float
    sl_price: float
    opened_at: str
    orders: list[Order] = field(default_factory=list)
    tp_price: float = 0.0
    tp_order_id: str | None = None
    tp_order_qty: float = 0.0
    sl_order_id: str | None = None
    fees_usdt: float = 0.0
    status: str = "open"  # open | closed
    close_price: float = 0.0
    close_reason: str = ""
    closed_at: str = ""
    realized_pnl: float = 0.0

    @property
    def filled_qty(self) -> float:
        return sum(o.filled_qty for o in self.orders)

    @property
    def avg_price(self) -> float:
        return average_price([(o.fill_price, o.filled_qty) for o in self.orders if o.filled_qty > 0])

    @property
    def filled_notional(self) -> float:
        return sum(o.fill_price * o.filled_qty for o in self.orders)

    @property
    def safety_orders_filled(self) -> int:
        return sum(1 for o in self.orders[1:] if o.filled_qty > 0)

    def pending_safety_orders(self) -> list[Order]:
        return [o for o in self.orders[1:] if o.filled_qty < o.qty * 0.999]

    def recompute_tp(self) -> None:
        self.tp_price = take_profit_price(self.avg_price, self.side, self.take_profit_pct)

    def close(self, price: float, reason: str, at: str, exit_fee: float) -> None:
        self.fees_usdt += exit_fee
        self.close_price = price
        self.close_reason = reason
        self.closed_at = at
        self.status = "closed"
        self.realized_pnl = pnl(self.side, self.avg_price, price, self.filled_qty) - self.fees_usdt

    def unrealized_pnl(self, price: float) -> float:
        return pnl(self.side, self.avg_price, price, self.filled_qty)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Deal":
        raw = dict(raw)
        raw["orders"] = [Order(**o) for o in raw.get("orders", [])]
        return cls(**raw)
