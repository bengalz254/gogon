"""Polymarket trading-fee model.

Polymarket charges takers (orders that cross the spread, like this bot's
fill-or-kill orders) a fee of

    fee_usd = shares * rate * (price * (1 - price)) ** exponent

with `rate` and `exponent` set per market (exponent is 1 for the standard
category schedule). The fee is paid in collateral on top of the price for
buys and comes out of the proceeds for sells. Makers pay nothing unless a
market says otherwise (`taker_only: false`). In dollar terms the fee peaks at
a 50c price: 100 shares at 50c in a 0.07-rate crypto market cost $1.75.

Where the numbers come from, in order:
  1. Polymarket's own market info (`/clob-markets/{condition_id}`, field
     `fd` = {r: rate, e: exponent, to: taker_only}), cached per market.
  2. The market's tags matched against `fees.category_taker_rates` in
     config/settings.yaml — the highest matching rate wins.
  3. `fees.default_taker_rate`, the highest known rate, so an unrecognized
     market is never under-costed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from bot.config import FeeConfig
from bot.market_data import MarketInfo

logger = logging.getLogger("polybot.fees")

# Re-check a market's fee terms this often; a failed lookup is retried sooner.
API_INFO_TTL_SECONDS = 6 * 3600
API_FAILURE_RETRY_SECONDS = 600


def taker_fee_usd(shares: float, price: float, rate: float, exponent: float = 1.0) -> float:
    """Fee in USD for trading `shares` at `price` under `rate` and `exponent`."""
    if shares <= 0 or rate <= 0:
        return 0.0
    p = min(max(price, 0.0), 1.0)
    return shares * rate * (p * (1.0 - p)) ** exponent


@dataclass(frozen=True)
class FeeSchedule:
    rate: float
    exponent: float = 1.0
    taker_only: bool = True
    source: str = "config"  # "api", "tags" or "default"

    def taker_fee(self, shares: float, price: float) -> float:
        return taker_fee_usd(shares, price, self.rate, self.exponent)

    def maker_fee(self, shares: float, price: float) -> float:
        return 0.0 if self.taker_only else self.taker_fee(shares, price)


def parse_fee_details(info) -> FeeSchedule | None:
    """FeeSchedule from a `/clob-markets` response, or None if it has no fee block."""
    if not isinstance(info, dict):
        return None
    fd = info.get("fd")
    if not isinstance(fd, dict) or fd.get("r") is None:
        return None
    try:
        rate = float(fd["r"])
        # Missing means the standard exponent; an explicit 0 (a flat fee per
        # share, the most expensive shape) is taken as given.
        exponent = 1.0 if fd.get("e") is None else float(fd["e"])
    except (TypeError, ValueError):
        return None
    if not 0.0 <= rate < 1.0:
        return None
    return FeeSchedule(rate=rate, exponent=exponent, taker_only=fd.get("to", True) is not False, source="api")


class FeeModel:
    def __init__(self, cfg: FeeConfig, market_info: Callable[[str], dict] | None = None):
        """`market_info(condition_id)` fetches Polymarket's market info (pass
        the CLOB client's get_clob_market_info); without it only the config
        is used."""
        self.cfg = cfg
        self._market_info = market_info
        self._category_rates = {
            name.strip().lower(): float(rate) for name, rate in cfg.category_taker_rates.items()
        }
        self._api_cache: dict[str, tuple[FeeSchedule | None, float]] = {}

    @classmethod
    def zero(cls) -> "FeeModel":
        """A fee-free model (tests, or markets you know charge no fees)."""
        return cls(FeeConfig(default_taker_rate=0.0, category_taker_rates={}))

    def schedule(self, market: MarketInfo) -> FeeSchedule:
        api = self._from_api(market.condition_id)
        if api is not None:
            return api
        matched = [
            self._category_rates[tag.strip().lower()]
            for tag in market.tags
            if tag.strip().lower() in self._category_rates
        ]
        if matched:
            return FeeSchedule(rate=max(matched), source="tags")
        return FeeSchedule(rate=self.cfg.default_taker_rate, source="default")

    def taker_rate(self, market: MarketInfo) -> float:
        return self.schedule(market).rate

    def _from_api(self, condition_id: str) -> FeeSchedule | None:
        if self._market_info is None or not condition_id:
            return None
        now = time.monotonic()
        cached = self._api_cache.get(condition_id)
        if cached is not None:
            schedule, fetched_at = cached
            ttl = API_INFO_TTL_SECONDS if schedule is not None else API_FAILURE_RETRY_SECONDS
            if now - fetched_at < ttl:
                return schedule
        try:
            schedule = parse_fee_details(self._market_info(condition_id))
        except Exception as exc:
            logger.debug("Fee lookup failed for %s (%s); using config rates", condition_id, type(exc).__name__)
            schedule = None
        self._api_cache[condition_id] = (schedule, now)
        return schedule
