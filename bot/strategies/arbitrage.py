"""Complete-set arbitrage: buy equal shares of every outcome in a binary
market when their combined cost — taker fees included — is reliably below $1.

Why this is the "safe default" strategy: a binary Polymarket market pays out
exactly $1 to the winning outcome's shares and $0 to the losing one. Holding
one YES share and one NO share therefore always resolves to exactly $1,
regardless of which side wins. If you can buy one of each for less than
$1 after fees, the difference is locked-in profit at resolution — the only
real risks are execution risk (one leg fills, the other doesn't) and
Polymarket changing its fees/rules. `fee_buffer` and `min_edge` keep a
safety margin around both.

Fees decide whether an opportunity is real: both legs are bought as a
taker, and near 50c prices the fee on one set is about rate * 0.5 — 3.5c in
a 0.07-rate crypto market, more than a typical 2c discount. So the edge is
always computed net of fees (see bot/fees.py).

Markets that hold taker orders before matching (sports, `seconds_delay`)
are skipped: a fill-or-kill leg's outcome isn't known when it's accepted.
"""
from __future__ import annotations

import logging
import uuid

from bot.config import ArbitrageConfig
from bot.fees import FeeModel, FeeSchedule
from bot.market_data import MarketInfo
from bot.risk import RiskManager
from bot.strategies.base import GetBook, Signal, round_down_shares

logger = logging.getLogger("polybot.strategy.arbitrage")


def guaranteed_profit_per_set(prices: list[float], fees: FeeSchedule) -> float:
    """Profit of one complete set (one share of every outcome) bought as a
    taker at `prices`: it pays exactly $1, and costs the prices plus the
    taker fee on each leg (charged in collateral on top of the price)."""
    return 1.0 - sum(prices) - sum(fees.taker_fee(1.0, p) for p in prices)


class ArbitrageStrategy:
    name = "arbitrage"

    def __init__(self, cfg: ArbitrageConfig, risk: RiskManager, fees: FeeModel):
        self.cfg = cfg
        self.risk = risk
        self.fees = fees

    def generate_signals(self, market: MarketInfo, get_book: GetBook) -> list[Signal]:
        if not self.cfg.enabled:
            return []
        # Only handles simple binary (two-outcome) markets for now.
        if len(market.tokens) != 2:
            return []
        if market.seconds_delay > 0 or not market.accepting_orders:
            return []

        token_a, token_b = market.tokens[0], market.tokens[1]
        book_a = get_book(token_a.token_id)
        book_b = get_book(token_b.token_id)

        if book_a.best_ask is None or book_b.best_ask is None:
            return []
        if book_a.best_ask_size <= 0 or book_b.best_ask_size <= 0:
            return []

        fees = self.fees.schedule(market)
        prices = [book_a.best_ask, book_b.best_ask]
        combined_ask = sum(prices)
        fee_per_set = sum(fees.taker_fee(1.0, p) for p in prices)
        edge = guaranteed_profit_per_set(prices, fees) - self.cfg.fee_buffer
        if edge < self.cfg.min_edge:
            return []

        max_usd = self.risk.max_affordable_usd(market.condition_id)
        if max_usd < self.risk.cfg.min_order_size_usd:
            return []

        # Arbitrage requires buying the SAME number of shares of both legs
        # (1 YES + 1 NO always resolves to $1). Size is bounded by: available
        # liquidity at the best ask on each leg, and the risk budget, which
        # has to cover the fees as well as the prices.
        cost_per_set = combined_ask + fee_per_set
        shares_by_liquidity = min(book_a.best_ask_size, book_b.best_ask_size)
        shares_by_budget = max_usd / cost_per_set
        shares = round_down_shares(min(shares_by_liquidity, shares_by_budget))

        # Each leg is its own order, so each must clear the minimum order size
        # (a lopsided market can make the cheap leg worth only cents).
        cost_usd = shares * cost_per_set
        if shares <= 0 or min(shares * p for p in prices) < self.risk.cfg.min_order_size_usd:
            return []

        group_id = str(uuid.uuid4())
        reason = (
            f"combined ask {combined_ask:.4f} + fees {fee_per_set:.4f}/set (rate {fees.rate:g}, {fees.source}); "
            f"net edge {edge:.4f} after {self.cfg.fee_buffer:.4f} buffer"
        )

        logger.info(
            "Arbitrage found in market %s: %s — buying %.2f shares each leg (~$%.2f incl. fees)",
            market.condition_id,
            market.question,
            shares,
            cost_usd,
        )

        # Thinner book first: it is the leg most likely to fail, and if the
        # first leg fails the executor doesn't send the second one.
        legs = sorted(
            [(token_a, book_a), (token_b, book_b)], key=lambda leg: leg[1].best_ask_size
        )
        return [
            Signal(
                strategy=self.name,
                market_id=market.condition_id,
                token_id=token.token_id,
                outcome=token.outcome,
                side="BUY",
                limit_price=book.best_ask,
                size_shares=shares,
                size_usd=shares * book.best_ask,
                reason=reason,
                group_id=group_id,
                fee_usd=fees.taker_fee(shares, book.best_ask),
                outcome_count=len(market.tokens),
            )
            for token, book in legs
        ]
