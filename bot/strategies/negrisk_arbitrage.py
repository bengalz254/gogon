"""Multi-outcome arbitrage: buy the YES of every outcome of a negRisk event
when all of them together cost less than $1 after taker fees.

Exactly one outcome of such an event resolves YES, so one YES share of each
pays exactly $1. bot/gamma.py only hands over events where "every outcome"
can be established (not augmented, no "Other" placeholder, all outcomes
trading) — buying YES on only some outcomes is a bet, not an arbitrage.

Riskier than the binary version: all legs go out in one request, but with
several legs the chance that some fill and others don't is higher, and a
partial set is a directional position (the bot alerts on it). The sets
can't be merged early either — they're held until the event resolves. Off
by default; run it in paper mode first.
"""
from __future__ import annotations

import logging
import uuid

from bot.config import NegRiskArbitrageConfig
from bot.fees import FeeModel
from bot.gamma import NegRiskEvent
from bot.risk import RiskManager
from bot.strategies.base import GetBook, Signal, round_down_shares

logger = logging.getLogger("polybot.strategy.negrisk")


class NegRiskArbitrageStrategy:
    name = "negrisk_arbitrage"

    def __init__(self, cfg: NegRiskArbitrageConfig, risk: RiskManager, fees: FeeModel):
        self.cfg = cfg
        self.risk = risk
        self.fees = fees

    def generate_signals(self, event: NegRiskEvent, get_book: GetBook) -> list[Signal]:
        if not self.cfg.enabled or len(event.outcomes) < 2:
            return []
        legs = []
        for market in event.outcomes:
            if market.seconds_delay > 0:
                return []  # a delayed taker leg's outcome isn't known when it's accepted
            yes = market.tokens[0]
            held = self.risk.positions.get(yes.token_id)
            if held is not None and held.set_id != event.set_id:
                return []  # shares held for another purpose would get mixed into the set
            book = get_book(yes.token_id)
            if book.best_ask is None or book.best_ask_size <= 0:
                return []
            legs.append((market, yes, book, self.fees.schedule(market)))

        prices = [book.best_ask for _, _, book, _ in legs]
        fee_per_set = sum(fees.taker_fee(1.0, book.best_ask) for _, _, book, fees in legs)
        cost_per_set = sum(prices) + fee_per_set
        edge = 1.0 - cost_per_set - self.cfg.fee_buffer
        if edge < self.cfg.min_edge:
            return []

        max_usd = self.risk.max_affordable_usd(event.set_id)
        shares = round_down_shares(min(min(book.best_ask_size for _, _, book, _ in legs), max_usd / cost_per_set))
        # Every leg is its own order and must clear the minimum order size.
        if shares <= 0 or min(shares * p for p in prices) < self.risk.cfg.min_order_size_usd:
            return []

        group_id = str(uuid.uuid4())
        reason = (
            f"{len(legs)} outcomes: YES asks sum {sum(prices):.4f} + fees {fee_per_set:.4f}/set; "
            f"net edge {edge:.4f} after {self.cfg.fee_buffer:.4f} buffer"
        )
        logger.info(
            "Multi-outcome arbitrage in %r: buying %.2f YES of each of %d outcomes (~$%.2f incl. fees)",
            event.title,
            shares,
            len(legs),
            shares * cost_per_set,
        )
        legs.sort(key=lambda leg: leg[2].best_ask_size)  # thinnest book first
        return [
            Signal(
                strategy=self.name,
                market_id=market.condition_id,
                token_id=yes.token_id,
                outcome=f"YES {market.question}"[:60],
                side="BUY",
                limit_price=book.best_ask,
                size_shares=shares,
                size_usd=shares * book.best_ask,
                reason=reason,
                group_id=group_id,
                fee_usd=fees.taker_fee(shares, book.best_ask),
                outcome_count=len(legs),
                set_id=event.set_id,
            )
            for market, yes, book, fees in legs
        ]
