"""Market making for Polymarket's liquidity rewards (pure logic, no I/O).

Polymarket pays daily rewards to resting limit orders that sit within a
market's `max_spread` of the midpoint and are at least `min_size` shares,
weighted toward orders closer to the midpoint and toward quoting both sides.
Makers also pay no trading fee (unless a market says otherwise) and get part
of the takers' fees back. The risk is being "picked off": a better-informed
trader fills our stale quote just before the price moves.

Quoting a binary market, in terms of its first ("primary") token P and the
other token O, whose prices add up to $1:
  - bid side (gain P exposure):  BUY P at b — or, when holding O, SELL O at
    1 - b, which is the same exposure change but recycles inventory;
  - ask side (shed P exposure):  SELL P at a when holding P, else BUY O at
    1 - a.
Selling inventory before buying the complement keeps capital from piling up
in complete sets. Quotes lean against the net inventory so fills tend to
flatten it, and a side stops quoting once net inventory hits the limit.

This module only decides *what* to quote; bot/quoting.py places the orders.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from bot.config import MarketMakerConfig
from bot.market_data import MarketInfo
from bot.orderbook import OrderBook

_EPS = 1e-9


@dataclass(frozen=True)
class Quote:
    token_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size: float


def floor_to_tick(price: float, tick: float) -> float:
    return round(math.floor(price / tick + _EPS) * tick, 6)


def ceil_to_tick(price: float, tick: float) -> float:
    return round(math.ceil(price / tick - _EPS) * tick, 6)


def quote_size(market: MarketInfo, book: OrderBook | None, cfg: MarketMakerConfig) -> float:
    """Shares per quote: the configured size, raised to what the market requires
    for an order to exist (min_order_size) and to earn rewards (min_size)."""
    return max(
        cfg.order_size,
        (market.rewards.min_size if market.rewards else None) or 0.0,
        market.min_order_size or 0.0,
        (book.min_order_size if book else None) or 0.0,
    )


def half_spread(market: MarketInfo, tick: float, cfg: MarketMakerConfig) -> float:
    max_spread = market.rewards.max_spread if market.rewards else None
    half = cfg.spread_fraction * max_spread if max_spread else cfg.default_half_spread
    return max(half, tick)


def compute_quotes(
    market: MarketInfo,
    book: OrderBook,
    inventory: dict[str, float],
    cfg: MarketMakerConfig,
) -> list[Quote]:
    """Quotes for one binary market, from the primary token's book.

    `inventory` maps token_id -> shares held. Returns [] when the book is
    one-sided or the midpoint is outside [min_mid, max_mid].
    """
    if len(market.tokens) != 2:
        return []
    primary, other = market.tokens[0].token_id, market.tokens[1].token_id
    bid, ask = book.best_bid, book.best_ask
    if bid is None or ask is None or ask <= bid:
        return []
    mid = (bid + ask) / 2
    if not cfg.min_mid <= mid <= cfg.max_mid:
        return []

    tick = book.tick_size or market.tick_size or 0.01
    half = half_spread(market, tick, cfg)
    held_primary, held_other = inventory.get(primary, 0.0), inventory.get(other, 0.0)
    net = held_primary - held_other
    lean = cfg.inventory_skew * half * max(-1.0, min(1.0, net / cfg.max_inventory))
    center = mid - lean

    # Post-only: never at or through the other side of the book.
    bid_price = min(floor_to_tick(center - half, tick), floor_to_tick(ask - tick, tick))
    ask_price = max(ceil_to_tick(center + half, tick), ceil_to_tick(bid + tick, tick))
    size = quote_size(market, book, cfg)

    quotes = []
    if net < cfg.max_inventory and tick - _EPS <= bid_price <= 1 - tick + _EPS:
        if held_other >= size:
            quotes.append(Quote(other, "SELL", round(1 - bid_price, 6), size))
        else:
            quotes.append(Quote(primary, "BUY", bid_price, size))
    if net > -cfg.max_inventory and tick - _EPS <= ask_price <= 1 - tick + _EPS:
        if held_primary >= size:
            quotes.append(Quote(primary, "SELL", ask_price, size))
        else:
            quotes.append(Quote(other, "BUY", round(1 - ask_price, 6), size))
    return quotes


def skip_reason(market: MarketInfo, cfg: MarketMakerConfig, now: datetime) -> str | None:
    """Why a market shouldn't be quoted right now (None if it's fine).

    Checked when picking markets and again on every refresh, so a market is
    dropped as its game start or end date approaches.
    """
    if len(market.tokens) != 2:
        return "not a binary market"
    if market.rewards is None:
        return "no liquidity rewards"
    if not market.accepting_orders or market.closed:
        return "not accepting orders"
    if market.seconds_delay > 0:
        return "delays taker orders (live sports)"
    if market.end_date is not None and market.end_date - now < timedelta(days=cfg.min_days_to_end):
        return "ends too soon"
    if market.game_start_time is not None and market.game_start_time - now < timedelta(hours=cfg.avoid_game_start_hours):
        return "game starts soon or has started"
    return None


def select_markets(
    markets: list[MarketInfo],
    get_book: Callable[[str], OrderBook | None],
    cfg: MarketMakerConfig,
    now: datetime,
    max_quote_usd: float,
) -> list[MarketInfo]:
    """The best reward markets to quote: eligible, midpoint inside the band,
    affordable at the reward minimum size, highest daily reward first."""
    eligible = [m for m in markets if skip_reason(m, cfg, now) is None]
    eligible.sort(key=lambda m: m.rewards.daily_rate if m.rewards else 0.0, reverse=True)
    chosen = []
    for market in eligible:
        book = get_book(market.tokens[0].token_id)
        mid = book.mid if book is not None else None
        if mid is None or not cfg.min_mid <= mid <= cfg.max_mid:
            continue
        # A quote on either side costs at most size * max(mid, 1 - mid).
        if quote_size(market, book, cfg) * max(mid, 1 - mid) > max_quote_usd:
            continue
        chosen.append(market)
        if len(chosen) >= cfg.max_markets:
            break
    return chosen


class MidTracker:
    """Recent midpoints per market, for the volatility guard."""

    def __init__(self, window_seconds: float):
        self.window = window_seconds
        self._history: dict[str, deque] = {}

    def add(self, market_id: str, mid: float, now: float) -> float:
        """Record a midpoint; returns the range (max - min) within the window."""
        history = self._history.setdefault(market_id, deque())
        history.append((now, mid))
        while history and now - history[0][0] > self.window:
            history.popleft()
        mids = [m for _, m in history]
        return max(mids) - min(mids)

    def reset(self, market_id: str) -> None:
        self._history.pop(market_id, None)
