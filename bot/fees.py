"""Polymarket taker-fee model.

Polymarket charges takers (orders that cross the spread, like this bot's
fill-or-kill orders) a fee of

    fee_usd = shares * rate * price * (1 - price)

where `rate` depends on the market's category. Makers pay nothing. In dollar
terms the fee peaks at a 50c price and shrinks toward zero near 0c / 100c,
so e.g. 100 shares at 50c in a 0.07-rate crypto market cost $1.75 in fees.

Rates come from config/settings.yaml (`fees:`) because Polymarket changes
them. A market is matched to a category through its tags; when several
categories match, the highest rate wins, and when none match the configured
default (the highest known rate) is used — so fees are never under-estimated.
"""
from __future__ import annotations

from bot.config import FeeConfig
from bot.market_data import MarketInfo


def taker_fee_usd(shares: float, price: float, rate: float) -> float:
    """Taker fee in USD for trading `shares` at `price` in a market with `rate`."""
    if shares <= 0 or rate <= 0:
        return 0.0
    p = min(max(price, 0.0), 1.0)
    return shares * rate * p * (1.0 - p)


class FeeModel:
    def __init__(self, cfg: FeeConfig):
        self.cfg = cfg
        self._category_rates = {
            name.strip().lower(): float(rate) for name, rate in cfg.category_taker_rates.items()
        }

    @classmethod
    def zero(cls) -> "FeeModel":
        """A fee-free model (tests, or markets you know charge no fees)."""
        return cls(FeeConfig(default_taker_rate=0.0, category_taker_rates={}))

    def taker_rate(self, market: MarketInfo) -> float:
        matched = [
            self._category_rates[tag.strip().lower()]
            for tag in market.tags
            if tag.strip().lower() in self._category_rates
        ]
        return max(matched) if matched else self.cfg.default_taker_rate
