"""Risk management: position sizing limits, exposure caps, and a daily-loss kill switch.

Pure logic, no network/IO — easy to unit test and to reason about before any
real money is at stake. Persistence lives in bot/state.py.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone

from bot.config import RiskConfig

# Tolerance for comparing dollar amounts against caps, so float noise like
# 25.000000000000004 > 25.0 doesn't reject an order sized exactly to a cap.
_EPS = 1e-9


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


@dataclass
class Position:
    market_id: str
    token_id: str
    outcome: str
    size: float  # shares held
    cost_usd: float  # total USD spent to acquire this position (fees included)
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Number of outcomes in the market (None if unknown). When every outcome
    # of a market is held, the overlapping shares form complete sets worth $1.
    outcome_count: int | None = None
    # Groups positions spread over several markets that together form
    # complete sets (the YES of every outcome of a multi-outcome event).
    # Exposure caps and complete-set valuation then work per group.
    set_id: str | None = None

    @property
    def avg_price(self) -> float:
        return self.cost_usd / self.size if self.size else 0.0

    @property
    def risk_key(self) -> str:
        """What exposure caps and complete sets are counted per."""
        return self.set_id or self.market_id


@dataclass
class MergeLeg:
    """One outcome's share of a merge: `size` shares returned for `proceeds_usd`."""

    token_id: str
    outcome: str
    size: float
    proceeds_usd: float
    pnl: float


def _complete_sets(positions: list["Position"]) -> float:
    """Complete sets among one market's positions: the shares held of every
    outcome, or 0 unless every outcome of the market is held."""
    if not positions:
        return 0.0
    outcome_count = positions[0].outcome_count
    if (
        outcome_count is None
        or len(positions) != outcome_count
        or any(p.outcome_count != outcome_count for p in positions)
    ):
        return 0.0
    return min(p.size for p in positions)


@dataclass
class RiskState:
    """Everything RiskManager needs to survive a restart."""

    day: date
    realized_pnl_today: float
    positions: list[Position]


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.positions: dict[str, Position] = {}  # keyed by token_id
        self.realized_pnl_today: float = 0.0
        # Latest sell-side price per token, set by update_marks().
        self._marks: dict[str, float] = {}
        self._day: date = _utc_today()

    # -- bookkeeping -------------------------------------------------
    def _roll_day_if_needed(self) -> None:
        today = _utc_today()
        if today != self._day:
            self._day = today
            self.realized_pnl_today = 0.0

    @property
    def total_exposure_usd(self) -> float:
        return sum(p.cost_usd for p in self.positions.values())

    def market_exposure_usd(self, key: str) -> float:
        """Exposure of one market (or one multi-market set, by its set_id)."""
        return sum(p.cost_usd for p in self.positions.values() if p.risk_key == key)

    @property
    def unrealized_pnl(self) -> float:
        """Mark-to-market P&L of the positions held right now, at the latest
        marks. Computed on demand so a position closed since the last
        update_marks() (e.g. a settled market) never counts twice."""
        return self.mark_to_market(self._marks)

    @property
    def daily_pnl(self) -> float:
        """Realized P&L today plus the current unrealized P&L of open positions."""
        self._roll_day_if_needed()
        return self.realized_pnl_today + self.unrealized_pnl

    @property
    def daily_loss_limit_hit(self) -> bool:
        return self.daily_pnl <= -abs(self.cfg.max_daily_loss_usd)

    # -- pre-trade checks ----------------------------------------------
    def can_open(self, market_id: str, proposed_usd: float) -> tuple[bool, str]:
        """Check whether a new position of `proposed_usd` in `market_id` is allowed.

        Returns (allowed, reason). reason is human-readable, empty if allowed.
        """
        self._roll_day_if_needed()

        if proposed_usd < self.cfg.min_order_size_usd - _EPS:
            return False, (
                f"order size ${proposed_usd:.2f} below minimum "
                f"${self.cfg.min_order_size_usd:.2f}"
            )

        if self.daily_loss_limit_hit:
            return False, (
                f"daily loss limit reached (realized ${self.realized_pnl_today:.2f} + "
                f"unrealized ${self.unrealized_pnl:.2f} <= -${self.cfg.max_daily_loss_usd:.2f}); "
                "no new positions until P&L recovers or UTC midnight"
            )

        market_exposure = self.market_exposure_usd(market_id)
        if market_exposure + proposed_usd > self.cfg.max_position_usd + _EPS:
            return False, (
                f"would exceed max_position_usd for market {market_id}: "
                f"${market_exposure:.2f} + ${proposed_usd:.2f} > ${self.cfg.max_position_usd:.2f}"
            )

        total = self.total_exposure_usd
        if total + proposed_usd > self.cfg.max_total_exposure_usd + _EPS:
            return False, (
                f"would exceed max_total_exposure_usd: "
                f"${total:.2f} + ${proposed_usd:.2f} > ${self.cfg.max_total_exposure_usd:.2f}"
            )

        return True, ""

    def max_affordable_usd(self, market_id: str) -> float:
        """Largest position (USD) currently allowed for this market, given caps."""
        self._roll_day_if_needed()
        if self.daily_loss_limit_hit:
            return 0.0
        per_market_room = max(0.0, self.cfg.max_position_usd - self.market_exposure_usd(market_id))
        total_room = max(0.0, self.cfg.max_total_exposure_usd - self.total_exposure_usd)
        return min(per_market_room, total_room)

    # -- fill recording --------------------------------------------------
    def record_open(
        self,
        market_id: str,
        token_id: str,
        outcome: str,
        size: float,
        cost_usd: float,
        outcome_count: int | None = None,
        set_id: str | None = None,
    ) -> None:
        existing = self.positions.get(token_id)
        if existing is None:
            self.positions[token_id] = Position(
                market_id=market_id,
                token_id=token_id,
                outcome=outcome,
                size=size,
                cost_usd=cost_usd,
                outcome_count=outcome_count,
                set_id=set_id,
            )
        else:
            existing.size += size
            existing.cost_usd += cost_usd
            if existing.outcome_count is None:
                existing.outcome_count = outcome_count

    def record_close(self, token_id: str, size: float, proceeds_usd: float) -> float:
        """Reduce/close a position, realize P&L, and return the realized P&L for this fill."""
        self._roll_day_if_needed()
        pos = self.positions.get(token_id)
        if pos is None or pos.size <= 0:
            return 0.0

        size = min(size, pos.size)
        cost_basis = pos.avg_price * size
        pnl = proceeds_usd - cost_basis

        pos.size -= size
        pos.cost_usd -= cost_basis
        if pos.size <= 1e-9:
            del self.positions[token_id]

        self.realized_pnl_today += pnl
        return pnl

    # -- mark-to-market ------------------------------------------------------
    def mark_to_market(self, marks: dict[str, float]) -> float:
        """Unrealized P&L of all open positions.

        `marks` maps token_id -> the price the position could be sold at now
        (normally the best bid). Tokens without a mark are valued at their
        average cost, i.e. contribute nothing. When a market's every outcome is
        held, the overlapping shares are complete sets and are valued at
        exactly $1 each (they can be merged back into $1 of collateral), so a
        hedged arbitrage position isn't shown as a loss just because of the
        bid/ask spread.
        """
        by_key: dict[str, list[Position]] = defaultdict(list)
        for pos in self.positions.values():
            by_key[pos.risk_key].append(pos)

        pnl = 0.0
        for positions in by_key.values():
            sets = _complete_sets(positions)
            value = sets * 1.0
            for p in positions:
                mark = marks.get(p.token_id)
                if mark is None:
                    mark = p.avg_price
                value += (p.size - sets) * mark
            pnl += value - sum(p.cost_usd for p in positions)
        return pnl

    def update_marks(self, marks: dict[str, float]) -> float:
        """Store current marks (they feed unrealized_pnl and the kill switch)."""
        self._marks = dict(marks)
        return self.unrealized_pnl

    # -- merging complete sets -------------------------------------------------
    def complete_sets(self, key: str) -> float:
        return _complete_sets([p for p in self.positions.values() if p.risk_key == key])

    def markets_with_complete_sets(self) -> dict[str, float]:
        """Mergeable complete sets per market. Sets spanning several markets
        (multi-outcome events) are left out: they can't be merged, only held
        until the event resolves."""
        keys = {p.market_id for p in self.positions.values() if p.set_id is None}
        return {k: sets for k in keys if (sets := self.complete_sets(k)) > 0}

    def record_merge(self, market_id: str, sets: float | None = None) -> list[MergeLeg]:
        """Turn complete sets back into $1 of collateral each, as Polymarket's
        Merge does. Every outcome's position shrinks by the merged amount at
        its average cost; the $1 per set is split across the outcomes in
        proportion to their cost so each one's P&L share is fair. Returns one
        MergeLeg per outcome (empty if there was nothing to merge)."""
        positions = [p for p in self.positions.values() if p.risk_key == market_id]
        if any(p.set_id is not None for p in positions):
            return []  # a multi-market set can't be merged
        available = _complete_sets(positions)
        sets = available if sets is None else min(sets, available)
        if sets <= 1e-9:
            return []
        costs = [p.avg_price * sets for p in positions]
        total_cost = sum(costs)
        legs = []
        for pos, cost in zip(positions, costs):
            proceeds = sets * (cost / total_cost) if total_cost > 0 else sets / len(positions)
            token_id, outcome = pos.token_id, pos.outcome
            pnl = self.record_close(token_id, sets, proceeds)
            legs.append(MergeLeg(token_id, outcome, sets, proceeds, pnl))
        return legs

    # -- persistence -------------------------------------------------------
    def snapshot(self) -> RiskState:
        self._roll_day_if_needed()
        return RiskState(
            day=self._day,
            realized_pnl_today=self.realized_pnl_today,
            positions=[replace(p) for p in self.positions.values()],
        )

    def restore(self, state: RiskState) -> None:
        """Load a snapshot taken earlier (e.g. before a restart).

        Open positions always carry over; today's realized P&L only if the
        snapshot is from the same UTC day, just as it would reset at midnight.
        """
        self.positions = {p.token_id: replace(p) for p in state.positions}
        self._day = _utc_today()
        self.realized_pnl_today = state.realized_pnl_today if state.day == self._day else 0.0
        self._marks = {}
