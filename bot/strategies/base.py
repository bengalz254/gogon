"""Shared strategy interface and trade signal type."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Protocol

from bot.market_data import BookLevel, MarketInfo

# Order sizes on the Polymarket CLOB have 0.01-share precision.
SHARE_STEP = 0.01


def round_down_shares(shares: float) -> float:
    """Round a share count down to the CLOB's precision.

    Rounding down (never up) keeps an order within the liquidity and risk
    budget it was sized against. The tiny epsilon stops float noise such as
    28.999999999999996 from costing a whole step.
    """
    if shares <= 0:
        return 0.0
    return math.floor(shares / SHARE_STEP + 1e-9) * SHARE_STEP


@dataclass
class Signal:
    strategy: str
    market_id: str
    token_id: str
    outcome: str
    side: str  # "BUY" or "SELL"
    limit_price: float  # worst-case/reference price used for sizing and order submission
    size_shares: float
    size_usd: float
    reason: str
    group_id: str | None = None  # links multi-leg trades (e.g. both arbitrage legs)
    # Estimated taker fee for this order (USD). BUY cost = size_usd + fee_usd;
    # SELL proceeds = size_usd - fee_usd.
    fee_usd: float = 0.0
    # Number of outcomes in the market, so holdings of every outcome can be
    # valued as complete sets (worth exactly $1 each).
    outcome_count: int | None = None


GetBook = Callable[[str], BookLevel]


class Strategy(Protocol):
    name: str

    def generate_signals(self, market: MarketInfo, get_book: GetBook) -> list[Signal]:
        """Inspect one market's order books and return zero or more trade signals."""
        ...
