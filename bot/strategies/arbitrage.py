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
"""
from __future__ import annotations

import logging
import uuid

from bot.config import ArbitrageConfig
from bot.fees import FeeModel, taker_fee_usd
from bot.market_data import MarketInfo
from bot.risk import RiskManager
from bot.strategies.base import GetBook, Signal, round_down_shares

logger = logging.getLogger("polybot.strategy.arbitrage")


def guaranteed_profit_per_set(prices: list[float], fee_rate: float) -> float:
    """Worst-case profit of one complete set (one share of every outcome)
    bought as a taker at `prices`.

    Polymarket may collect the taker fee in USD on top of the price, or by
    delivering fewer shares than bought. The two give different payouts, so
    this returns the lower of them:
      - fee in USD:    1 - sum(p) - rate * sum(p * (1 - p))
      - fee in shares: each leg keeps 1 - rate * (1 - p) shares per share
                       bought, so the scarcest leg bounds the $1 payout:
                       min(1 - rate * (1 - p)) - sum(p)
    """
    total = sum(prices)
    fee_in_usd = 1.0 - total - sum(taker_fee_usd(1.0, p, fee_rate) for p in prices)
    fee_in_shares = min(1.0 - fee_rate * (1.0 - p) for p in prices) - total
    return min(fee_in_usd, fee_in_shares)


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

        token_a, token_b = market.tokens[0], market.tokens[1]
        book_a = get_book(token_a.token_id)
        book_b = get_book(token_b.token_id)

        if book_a.best_ask is None or book_b.best_ask is None:
            return []
        if book_a.best_ask_size <= 0 or book_b.best_ask_size <= 0:
            return []

        rate = self.fees.taker_rate(market)
        prices = [book_a.best_ask, book_b.best_ask]
        combined_ask = sum(prices)
        fee_per_set = sum(taker_fee_usd(1.0, p, rate) for p in prices)
        edge = guaranteed_profit_per_set(prices, rate) - self.cfg.fee_buffer
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
            f"combined ask {combined_ask:.4f} + fees {fee_per_set:.4f}/set (rate {rate:g}); "
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
                fee_usd=taker_fee_usd(shares, book.best_ask, rate),
                outcome_count=len(market.tokens),
            )
            for token, book in legs
        ]
