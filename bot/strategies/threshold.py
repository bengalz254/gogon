"""Simple mean-reversion strategy (disabled by default).

Tracks a rolling average of each token's midpoint price. Buys when price
drops meaningfully below its recent average, and closes the position when
it rises back above average. Unlike arbitrage this is directional and can
lose money if a market trends rather than reverts — keep it disabled unless
you understand and accept that risk, and size it conservatively via
risk.max_position_usd.
"""
from __future__ import annotations

import logging
from collections import deque

from bot.config import ThresholdConfig
from bot.fees import FeeModel, taker_fee_usd
from bot.market_data import MarketInfo
from bot.risk import RiskManager
from bot.strategies.base import GetBook, Signal, round_down_shares

logger = logging.getLogger("polybot.strategy.threshold")


class ThresholdStrategy:
    name = "threshold"

    def __init__(self, cfg: ThresholdConfig, risk: RiskManager, fees: FeeModel):
        self.cfg = cfg
        self.risk = risk
        self.fees = fees
        self._history: dict[str, deque] = {}

    def _rolling_avg(self, token_id: str, price: float) -> float:
        hist = self._history.setdefault(token_id, deque(maxlen=self.cfg.lookback_ticks))
        hist.append(price)
        return sum(hist) / len(hist)

    def generate_signals(self, market: MarketInfo, get_book: GetBook) -> list[Signal]:
        if not self.cfg.enabled:
            return []

        signals: list[Signal] = []
        rate = self.fees.taker_rate(market)
        for token in market.tokens:
            book = get_book(token.token_id)
            if book.best_bid is None or book.best_ask is None:
                continue

            mid = (book.best_bid + book.best_ask) / 2
            avg = self._rolling_avg(token.token_id, mid)
            if avg <= 0:
                continue

            existing = self.risk.positions.get(token.token_id)
            drop_pct = (avg - mid) / avg
            rise_pct = (mid - avg) / avg

            if drop_pct >= self.cfg.buy_drop_pct:
                max_usd = self.risk.max_affordable_usd(market.condition_id)
                if max_usd >= self.risk.cfg.min_order_size_usd and book.best_ask > 0:
                    # The budget has to cover the taker fee as well as the price.
                    fee_per_share = taker_fee_usd(1.0, book.best_ask, rate)
                    shares = round_down_shares(max_usd / (book.best_ask + fee_per_share))
                    signals.append(
                        Signal(
                            strategy=self.name,
                            market_id=market.condition_id,
                            token_id=token.token_id,
                            outcome=token.outcome,
                            side="BUY",
                            limit_price=book.best_ask,
                            size_shares=shares,
                            size_usd=shares * book.best_ask,
                            reason=(
                                f"mid {mid:.3f} is {drop_pct:.1%} below "
                                f"{self.cfg.lookback_ticks}-tick avg {avg:.3f}"
                            ),
                            fee_usd=shares * fee_per_share,
                            outcome_count=len(market.tokens),
                        )
                    )
            elif rise_pct >= self.cfg.sell_rise_pct and existing and existing.size > 0:
                signals.append(
                    Signal(
                        strategy=self.name,
                        market_id=market.condition_id,
                        token_id=token.token_id,
                        outcome=token.outcome,
                        side="SELL",
                        limit_price=book.best_bid,
                        size_shares=existing.size,
                        size_usd=existing.size * book.best_bid,
                        reason=f"mid {mid:.3f} is {rise_pct:.1%} above avg {avg:.3f}; closing",
                        fee_usd=taker_fee_usd(existing.size, book.best_bid, rate),
                        outcome_count=len(market.tokens),
                    )
                )
        return signals
