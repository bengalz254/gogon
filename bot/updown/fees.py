"""Fee model for Polymarket crypto Up/Down markets.

Taker fee per share = p * rate * (p * (1 - p)) ** exponent, in USDC. With the
defaults (rate 0.25, exponent 2) that is ~0.78c per share at 50c (1.56% of
notional) and almost nothing near 0 or 1. Makers pay `maker_rate` (0 by
default). Verify these numbers against Polymarket's current fee docs and
adjust config/updown.yaml `fees:` if they changed.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.updown.config import FeesConfig


@dataclass
class FeeModel:
    taker_rate: float = 0.25
    taker_exponent: float = 2.0
    maker_rate: float = 0.0

    @classmethod
    def from_config(cls, cfg: FeesConfig) -> "FeeModel":
        return cls(cfg.taker_rate, cfg.taker_exponent, cfg.maker_rate)

    def _curve(self, price: float, rate: float) -> float:
        if rate <= 0 or price <= 0 or price >= 1:
            return 0.0
        return price * rate * (price * (1.0 - price)) ** self.taker_exponent

    def taker_per_share(self, price: float) -> float:
        return self._curve(price, self.taker_rate)

    def maker_per_share(self, price: float) -> float:
        return self._curve(price, self.maker_rate)

    def per_share(self, price: float, is_maker: bool) -> float:
        return self.maker_per_share(price) if is_maker else self.taker_per_share(price)
