"""Decision logic for one 5-minute window: buy mispriced shares, sell overpriced ones.

Pure logic, no IO. The engine hands in a Snapshot (spot, strike, time left,
volatility, both order books) plus what we already hold in this window, and
gets back at most one Decision per tick.

The edge being hunted is simple: the model's fair value for a share vs the
price someone is asking for it, after taker fees. Directional guessing at the
start of a window is deliberately not part of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from updown.config import FeeConfig, ModelConfig, SizingConfig, StrategyConfig
from updown.model import fair_prob_up, fair_prob_up_twap, kelly_fraction, taker_fee_per_share

UP = "Up"
DOWN = "Down"


@dataclass
class Level:
    price: float
    size: float  # shares


@dataclass
class Book:
    bids: list[Level] = field(default_factory=list)  # best (highest) first
    asks: list[Level] = field(default_factory=list)  # best (lowest) first

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        return self.best_ask if self.asks else self.best_bid

    @classmethod
    def from_raw(cls, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> "Book":
        return cls(
            bids=sorted((Level(p, s) for p, s in bids if s > 0), key=lambda l: -l.price),
            asks=sorted((Level(p, s) for p, s in asks if s > 0), key=lambda l: l.price),
        )


@dataclass
class Snapshot:
    spot: float
    strike: float
    seconds_left: float
    sigma: float
    books: dict[str, Book]  # keyed by UP / DOWN
    # Average price so far inside the closing TWAP window (None before it starts).
    twap_so_far: float | None = None


@dataclass
class WindowPosition:
    shares: dict[str, float] = field(default_factory=lambda: {UP: 0.0, DOWN: 0.0})
    cost_usd: dict[str, float] = field(default_factory=lambda: {UP: 0.0, DOWN: 0.0})
    entries: int = 0

    @property
    def total_cost_usd(self) -> float:
        return sum(self.cost_usd.values())


@dataclass
class Decision:
    side: str  # "BUY" or "SELL"
    outcome: str  # UP or DOWN
    limit_price: float  # worst price we accept
    shares: float
    usd: float  # sum(price * shares) across the levels we expect to hit
    fee_usd: float
    fair_prob: float
    reason: str


class UpDownStrategy:
    def __init__(self, strategy: StrategyConfig, model: ModelConfig, fees: FeeConfig, sizing: SizingConfig):
        self.cfg = strategy
        self.model = model
        self.fees = fees
        self.sizing = sizing

    def fee(self, price: float) -> float:
        return taker_fee_per_share(price, self.fees.fee_rate, self.fees.fee_exponent)

    def prob_up(self, snap: Snapshot, sigma_scale: float = 1.0) -> float:
        if self.model.twap_window_s > 0:
            return fair_prob_up_twap(
                snap.spot,
                snap.strike,
                snap.seconds_left,
                snap.sigma * sigma_scale,
                self.model.twap_window_s,
                observed_avg=snap.twap_so_far,
                basis_sd=self.model.basis_sd,
                vol_multiplier=self.model.vol_multiplier,
            )
        return fair_prob_up(
            snap.spot,
            snap.strike,
            snap.seconds_left,
            snap.sigma * sigma_scale,
            basis_sd=self.model.basis_sd,
            vol_multiplier=self.model.vol_multiplier,
        )

    def conservative_probs(self, snap: Snapshot) -> dict[str, float]:
        """Each side's probability under whichever volatility is worst for it.

        Only a real price move (spot away from strike) gives an edge that
        survives both ends of the band; a mere difference of opinion about
        volatility doesn't.
        """
        u = self.model.vol_uncertainty
        ups = [self.prob_up(snap, 1.0 / (1.0 + u)), self.prob_up(snap, 1.0 + u)]
        return {UP: min(ups), DOWN: 1.0 - max(ups)}

    def decide(
        self,
        snap: Snapshot,
        pos: WindowPosition,
        room_usd: float,
        side_room: dict[str, float] | None = None,
        seconds_since_entry: float | None = None,
    ) -> tuple[Decision | None, str]:
        """Return (decision, why).

        room_usd: how much more risk may open now, overall.
        side_room: how much more may open on each side across all coins.
        seconds_since_entry: time since this window's last entry, if any.
        """
        if snap.seconds_left < self.cfg.min_seconds_left:
            return None, f"too close to expiry ({snap.seconds_left:.0f}s left)"

        p_up = self.prob_up(snap)
        probs = {UP: p_up, DOWN: 1.0 - p_up}

        exit_decision = self._maybe_exit(snap, pos, probs)
        if exit_decision:
            return exit_decision, exit_decision.reason

        if snap.seconds_left > self.cfg.max_seconds_left:
            return None, f"too early in window ({snap.seconds_left:.0f}s left)"
        if pos.entries >= self.cfg.max_entries_per_window:
            return None, "max entries for this window reached"
        if seconds_since_entry is not None and seconds_since_entry < self.cfg.min_seconds_between_entries:
            return None, f"just entered; waiting {self.cfg.min_seconds_between_entries - seconds_since_entry:.0f}s before adding"

        window_room = self.sizing.max_window_exposure_usd - pos.total_cost_usd
        budget_cap = min(self.sizing.max_bet_usd, window_room, room_usd)
        if budget_cap < self.sizing.min_order_usd:
            return None, f"no room to add risk (${budget_cap:.2f} available)"

        entry_probs = self.conservative_probs(snap)
        best: Decision | None = None
        reasons = []
        for outcome in (UP, DOWN):
            other = DOWN if outcome == UP else UP
            if pos.shares[other] > 0:
                reasons.append(f"{outcome}: already hold {other}")
                continue
            cap = budget_cap if side_room is None else min(budget_cap, side_room.get(outcome, 0.0))
            if cap < self.sizing.min_order_usd:
                reasons.append(f"{outcome}: same-direction limit across coins reached")
                continue
            decision, why = self._entry(outcome, entry_probs[outcome], snap.books.get(outcome), cap)
            reasons.append(f"{outcome}: {why}")
            if decision and (best is None or self._edge(decision) > self._edge(best)):
                best = decision
        if best:
            return best, best.reason
        return None, "; ".join(reasons)

    # ------------------------------------------------------------------
    def _edge(self, d: Decision) -> float:
        return d.fair_prob - (d.usd + d.fee_usd) / d.shares

    def _entry(self, outcome: str, q: float, book: Book | None, budget_cap: float) -> tuple[Decision | None, str]:
        if book is None or not book.asks:
            return None, "no asks"
        mid = book.mid
        if mid is not None and abs(q - mid) > self.cfg.max_model_market_gap:
            return None, f"model {q:.2f} vs market {mid:.2f} too far apart; distrusting our data"
        if mid is not None and self.cfg.market_weight > 0:
            q = (1 - self.cfg.market_weight) * q + self.cfg.market_weight * mid

        # Kelly stake from the best price we could get; later levels are worse,
        # so this is the most we'd ever want.
        a0 = book.asks[0].price
        f = kelly_fraction(q, a0 + self.fee(a0))
        budget = min(budget_cap, self.sizing.kelly_fraction * f * self.sizing.bankroll_usd)

        shares = usd = fee_usd = 0.0
        limit = None
        for lvl in book.asks:
            x = lvl.price
            if not (self.cfg.min_price <= x <= self.cfg.max_price):
                break
            if q - x - self.fee(x) < self.cfg.min_edge:
                break
            take = min(lvl.size, (budget - usd - fee_usd) / (x + self.fee(x)))
            if take <= 0:
                break
            shares += take
            usd += take * x
            fee_usd += take * self.fee(x)
            limit = x

        if limit is None:
            fee0 = self.fee(a0)
            return None, f"ask {a0:.3f}+fee {fee0:.3f} vs fair {q:.3f}: edge {q - a0 - fee0:+.3f} < {self.cfg.min_edge}"
        if usd < self.sizing.min_order_usd:
            return None, f"edge found but stake ${usd:.2f} below minimum (kelly f={f:.3f})"

        avg = usd / shares
        return (
            Decision(
                side="BUY",
                outcome=outcome,
                limit_price=limit,
                shares=shares,
                usd=usd,
                fee_usd=fee_usd,
                fair_prob=q,
                reason=f"fair {q:.3f} vs avg ask {avg:.3f} (+fee {fee_usd / shares:.3f}) -> edge {q - avg - fee_usd / shares:+.3f}/share",
            ),
            "edge",
        )

    def _maybe_exit(self, snap: Snapshot, pos: WindowPosition, probs: dict[str, float]) -> Decision | None:
        for outcome in (UP, DOWN):
            held = pos.shares[outcome]
            book = snap.books.get(outcome)
            if held <= 0 or book is None or not book.bids:
                continue
            q = probs[outcome]
            shares = usd = fee_usd = 0.0
            limit = None
            for lvl in book.bids:
                x = lvl.price
                if x - self.fee(x) - q < self.cfg.exit_margin:
                    break
                take = min(lvl.size, held - shares)
                if take <= 0:
                    break
                shares += take
                usd += take * x
                fee_usd += take * self.fee(x)
                limit = x
            if limit is not None and shares > 0:
                return Decision(
                    side="SELL",
                    outcome=outcome,
                    limit_price=limit,
                    shares=shares,
                    usd=usd,
                    fee_usd=fee_usd,
                    fair_prob=q,
                    reason=f"bid {usd / shares:.3f} (-fee) beats fair {q:.3f} by >= {self.cfg.exit_margin}; selling to the market",
                )
        return None
