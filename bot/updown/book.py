"""Full-depth order book for one outcome token, maintained from CLOB
websocket snapshots ("book") and level updates ("price_change")."""
from __future__ import annotations

import math
from dataclasses import dataclass, field


def _key(price: float) -> float:
    return round(float(price), 6)


@dataclass
class OrderBook:
    token_id: str
    bids: dict = field(default_factory=dict)  # price -> size
    asks: dict = field(default_factory=dict)
    tick_size: float = 0.01
    has_snapshot: bool = False
    updated_at: float = 0.0

    # -- updates --------------------------------------------------------------
    def apply_snapshot(self, bids, asks, ts: float) -> None:
        self.bids = {_key(p): float(s) for p, s in bids if float(s) > 0}
        self.asks = {_key(p): float(s) for p, s in asks if float(s) > 0}
        self.has_snapshot = True
        self.updated_at = ts

    def apply_level(self, side: str, price: float, size: float, ts: float) -> None:
        """side: "BUY" (bid level) or "SELL" (ask level); size 0 removes it."""
        levels = self.bids if side.upper() == "BUY" else self.asks
        k = _key(price)
        if size <= 0:
            levels.pop(k, None)
        else:
            levels[k] = float(size)
        self.updated_at = ts

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.has_snapshot = False

    # -- queries --------------------------------------------------------------
    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def best_bid_size(self) -> float:
        b = self.best_bid
        return self.bids[b] if b is not None else 0.0

    @property
    def best_ask_size(self) -> float:
        a = self.best_ask
        return self.asks[a] if a is not None else 0.0

    @property
    def mid(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return (a + b) / 2.0

    @property
    def spread(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return a - b

    def size_at(self, side: str, price: float) -> float:
        levels = self.bids if side.upper() == "BUY" else self.asks
        return levels.get(_key(price), 0.0)

    def asks_up_to(self, limit: float) -> list[tuple[float, float]]:
        """Ask levels priced <= limit, cheapest first."""
        return sorted((p, s) for p, s in self.asks.items() if p <= limit + 1e-9)

    def bids_down_to(self, limit: float) -> list[tuple[float, float]]:
        return sorted(((p, s) for p, s in self.bids.items() if p >= limit - 1e-9), reverse=True)

    def depth_asks(self, limit: float) -> float:
        return sum(s for _, s in self.asks_up_to(limit))

    def vwap_buy(self, shares: float, limit: float = math.inf) -> tuple[float, float]:
        """(filled_shares, avg_price) for buying up to `shares` at <= limit."""
        filled = cost = 0.0
        for p, s in self.asks_up_to(limit):
            take = min(s, shares - filled)
            if take <= 0:
                break
            filled += take
            cost += take * p
        return filled, (cost / filled if filled else 0.0)
