"""Per-window state machine.

    UPCOMING --(t >= start, price_to_beat locked)--> LIVE
    LIVE     --(t >= end)--------------------------> CLOSED
    CLOSED   --(provisional + official outcome)----> SETTLED

price_to_beat is locked at t=0 from the oracle stream (or taken from Gamma
when it publishes one). A window whose opening price we could not observe
is never traded: without the strike there is no model, and without the
model the bot would just be gambling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

UP, DOWN = "Up", "Down"
OUTCOMES = (UP, DOWN)


def opposite(outcome: str) -> str:
    return DOWN if outcome == UP else UP


@dataclass(frozen=True)
class WindowSpec:
    window_id: str  # Gamma slug, e.g. btc-updown-5m-1760000000
    asset: str
    interval_s: int
    start: float
    end: float
    condition_id: str
    up_token: str
    down_token: str
    tick_size: float = 0.01
    min_order_size: float = 5.0
    neg_risk: bool = False
    question: str = ""

    def token(self, outcome: str) -> str:
        return self.up_token if outcome == UP else self.down_token

    def outcome_of(self, token_id: str) -> str | None:
        if token_id == self.up_token:
            return UP
        if token_id == self.down_token:
            return DOWN
        return None


class Phase(str, Enum):
    UPCOMING = "upcoming"
    LIVE = "live"
    CLOSED = "closed"
    SETTLED = "settled"


@dataclass
class Holding:
    shares: float = 0.0
    cost: float = 0.0  # USDC paid including fees

    @property
    def avg_price(self) -> float:
        return self.cost / self.shares if self.shares else 0.0


@dataclass
class WindowState:
    spec: WindowSpec
    phase: Phase = Phase.UPCOMING
    ptb: float | None = None
    ptb_source: str = ""
    # Both candidate strikes from our oracle feed, kept for the settlement-rule
    # check in the report: 60s TWAP before the open, and the price at the open.
    ptb_twap: float | None = None
    ptb_last: float | None = None
    official_ptb: float | None = None
    # (strategy, outcome) -> Holding
    holdings: dict = field(default_factory=dict)
    entries: dict = field(default_factory=dict)  # strategy -> count of take orders sent
    last_entry_ts: dict = field(default_factory=dict)  # strategy -> ts
    notes: dict = field(default_factory=dict)  # strategy scratch space
    last_model: object = None  # ModelSnapshot
    last_snapshot_log: float = 0.0
    tradable_reason: str = "not started"
    # settlement
    settle_twap: float | None = None
    settle_last: float | None = None
    provisional_winner: str | None = None
    official_winner: str | None = None
    closed_at: float | None = None
    booked: bool = False  # P&L booked (journal + risk)
    booked_winner: str | None = None
    pnl: dict = field(default_factory=dict)  # strategy -> realized P&L

    @property
    def window_id(self) -> str:
        return self.spec.window_id

    @property
    def asset(self) -> str:
        return self.spec.asset

    def holding(self, strategy: str, outcome: str) -> Holding:
        return self.holdings.setdefault((strategy, outcome), Holding())

    def shares(self, outcome: str, strategy: str | None = None) -> float:
        return sum(
            h.shares for (s, o), h in self.holdings.items()
            if o == outcome and (strategy is None or s == strategy)
        )

    def cost(self, strategy: str | None = None) -> float:
        return sum(h.cost for (s, _), h in self.holdings.items() if strategy is None or s == strategy)

    def strategies_held(self) -> set:
        return {s for (s, _), h in self.holdings.items() if h.shares > 0}

    @property
    def winner(self) -> str | None:
        return self.official_winner or self.provisional_winner
