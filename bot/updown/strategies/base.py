"""Strategy interface for the Up/Down engine.

A strategy looks at one window (plus market-wide context) and returns
*intents*, never orders:

- Take(...)  -- cross the spread now (FAK/FOK), paying taker fees.
- Quote(...) -- "I want a resting maker bid like this". Quotes are
  declarative: return the same key every step to keep it alive; stop
  returning it and the engine cancels it. The engine reconciles quotes with
  live orders, and the risk layer sizes and vetoes everything.

Edge convention (matches the design notes):

    edge = p_model - p_market - fee - half_spread - slippage
         = p_win - ask - taker_fee(ask) - slippage      (for a taker buy)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from bot.updown.book import OrderBook
from bot.updown.fees import FeeModel
from bot.updown.mathutil import floor_to_tick
from bot.updown.model import ModelSnapshot
from bot.updown.window import UP, WindowSpec, WindowState, opposite


@dataclass
class Take:
    outcome: str
    limit_price: float  # worst price we accept
    p_win: float  # conservative probability used for sizing
    edge: float  # at the current best ask, net of fees and slippage
    reason: str
    max_usd: float | None = None
    max_shares: float | None = None
    tif: str = "FAK"
    meta: dict = field(default_factory=dict)


@dataclass
class Quote:
    key: str  # stable per strategy+window, e.g. "late_bid"
    outcome: str
    price: float
    p_win: float
    reason: str
    shares: float | None = None  # desired resting size; None = let risk size it
    max_usd: float | None = None
    # Hard ceiling from the strategy's edge requirement. A live quote above it
    # is replaced immediately; otherwise small moves are ignored (hysteresis).
    limit: float | None = None
    # fixed_size: use `shares` as-is (still capped by risk) instead of Kelly.
    # For legs whose edge isn't directional, e.g. the barbell's base pair.
    fixed_size: bool = False
    meta: dict = field(default_factory=dict)


@dataclass
class QuoteView:
    """What a strategy can see about one of its live quotes."""

    key: str
    outcome: str
    price: float
    shares: float  # original size
    filled: float


@dataclass
class StrategyContext:
    now: float
    spec: WindowSpec
    window: WindowState
    model: ModelSnapshot
    up_book: OrderBook
    down_book: OrderBook
    fees: FeeModel
    params: object  # the strategy's config with overrides applied
    session: str
    strategy: str
    slippage: float
    market_blend: float
    # asset -> move since the window slot opened (spot/open - 1), all watched assets
    slot_deltas: dict = field(default_factory=dict)
    my_quotes: dict = field(default_factory=dict)  # key -> QuoteView
    asset_return: Callable[[str, float], float | None] = lambda asset, lookback: None
    spot_change: Callable[[float], float | None] = lambda lookback: None
    remodel: Callable[..., ModelSnapshot | None] = lambda **kw: None

    # -- books & prices ---------------------------------------------------------
    def book(self, outcome: str) -> OrderBook:
        return self.up_book if outcome == UP else self.down_book

    @property
    def tick(self) -> float:
        return max(self.up_book.tick_size, self.down_book.tick_size)

    def p_market(self, outcome: str) -> float | None:
        """Market-implied probability: the outcome's mid, else 1 - other mid."""
        mid = self.book(outcome).mid
        if mid is not None:
            return mid
        other = self.book(opposite(outcome)).mid
        return None if other is None else 1.0 - other

    def p_win(self, outcome: str, model: ModelSnapshot | None = None) -> float:
        """Conservative (vol-band) model probability, shrunk toward the market."""
        m = model or self.model
        p = m.p_conservative(outcome)
        pm = self.p_market(outcome)
        if pm is None or self.market_blend <= 0:
            return p
        return (1.0 - self.market_blend) * p + self.market_blend * pm

    # -- edge math ----------------------------------------------------------------
    def taker_edge(self, outcome: str, p: float, price: float | None = None) -> float | None:
        ask = self.book(outcome).best_ask if price is None else price
        if ask is None:
            return None
        return p - ask - self.fees.taker_per_share(ask) - self.slippage

    def max_taker_price(self, p: float, min_edge: float) -> float | None:
        """Highest tick price whose taker edge is still >= min_edge."""
        tick = self.tick
        price = floor_to_tick(p - min_edge - self.slippage, tick)
        while price >= tick:
            if p - price - self.fees.taker_per_share(price) - self.slippage >= min_edge - 1e-12:
                return price
            price = floor_to_tick(price - tick, tick)
        return None

    def edge_breakdown(self, outcome: str, p: float) -> str:
        book = self.book(outcome)
        ask, bid = book.best_ask, book.best_bid
        if ask is None:
            return f"p={p:.3f} no ask"
        mid = book.mid if book.mid is not None else ask
        half_spread = (ask - bid) / 2.0 if bid is not None else 0.0
        fee = self.fees.taker_per_share(ask)
        edge = p - mid - half_spread - fee - self.slippage
        return (
            f"p_win={p:.3f} p_mkt={mid:.3f} half_spread={half_spread:.3f} "
            f"fee={fee:.4f} slip={self.slippage:.3f} edge={edge:+.3f}"
        )

    def model_brief(self, m: ModelSnapshot | None = None) -> str:
        m = m or self.model
        sig_ann = m.sigma * math.sqrt(365.0 * 24 * 3600)
        return (
            f"{self.spec.asset} rem={m.remaining:.0f}s delta={m.delta * 100:+.3f}% "
            f"p_up={m.p_up:.3f}[{m.p_up_lo:.3f},{m.p_up_hi:.3f}] z={m.z:+.2f} "
            f"vol={sig_ann:.0%} twap_n={m.realized_n}"
        )

    # -- this strategy's state in this window ------------------------------------
    def held(self, outcome: str) -> float:
        return self.window.shares(outcome, self.strategy)

    @property
    def entries(self) -> int:
        return self.window.entries.get(self.strategy, 0)

    @property
    def last_entry_ts(self) -> float | None:
        return self.window.last_entry_ts.get(self.strategy)

    @property
    def notes(self) -> dict:
        return self.window.notes.setdefault(self.strategy, {})


class UpDownStrategy:
    name = "base"

    def __init__(self, cfg):
        self.cfg = cfg

    def evaluate(self, ctx: StrategyContext) -> list:
        raise NotImplementedError
