"""Market discovery and order-book helpers.

Polymarket's public market-listing payloads have some field-naming variance
across endpoints/versions, so parsing here is deliberately defensive: unknown
or missing fields fall back to safe defaults and are logged once rather than
crashing the bot.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bot.config import MarketFilterConfig
from bot.orderbook import BookLevel, OrderBook  # noqa: F401  (BookLevel re-exported)

logger = logging.getLogger("polybot.market_data")


@dataclass
class TokenInfo:
    token_id: str
    outcome: str


@dataclass
class RewardInfo:
    """A market's liquidity-rewards terms (from the `rewards` field)."""

    # Smallest order size (shares) that earns rewards.
    min_size: float | None = None
    # Furthest a quote may sit from the midpoint and still earn rewards, in
    # price units (0.035 = 3.5c).
    max_spread: float | None = None
    # Total reward paid per day for this market, in USD.
    daily_rate: float = 0.0


@dataclass
class MarketInfo:
    condition_id: str
    question: str
    tokens: list[TokenInfo] = field(default_factory=list)
    active: bool = True
    closed: bool = False
    volume_usd: float = 0.0
    liquidity_usd: float = 0.0
    # Category labels (e.g. "Crypto", "Sports") — used to pick the taker-fee rate.
    tags: list[str] = field(default_factory=list)
    end_date: datetime | None = None
    # Sports: when the game starts. Odds jump during live play.
    game_start_time: datetime | None = None
    accepting_orders: bool = True
    # Seconds taker orders are held before matching (sports markets). A
    # fill-or-kill order's outcome isn't known when it's accepted.
    seconds_delay: float = 0.0
    min_order_size: float | None = None
    tick_size: float | None = None
    neg_risk: bool = False
    neg_risk_market_id: str | None = None
    rewards: RewardInfo | None = None


def _first(raw: dict, *keys):
    for k in keys:
        if raw.get(k) is not None:
            return raw[k]
    return None


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value) -> datetime | None:
    """ISO-8601 timestamp -> aware UTC datetime (None if missing or unparseable)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_rewards(raw: dict) -> RewardInfo | None:
    rewards = raw.get("rewards")
    if not isinstance(rewards, dict):
        return None
    max_spread = _float_or_none(rewards.get("max_spread"))
    if max_spread is not None:
        # Published in cents (3.5 = 3.5c); a value under 0.2 is already a price.
        max_spread = max_spread / 100 if max_spread >= 0.2 else max_spread
        if max_spread <= 0:
            max_spread = None
    daily_rate = _float_or_none(rewards.get("daily_rate")) or 0.0
    for rate in rewards.get("rates") or []:
        if isinstance(rate, dict):
            daily_rate += _float_or_none(rate.get("rewards_daily_rate")) or 0.0
    return RewardInfo(
        min_size=_float_or_none(rewards.get("min_size")),
        max_spread=max_spread,
        daily_rate=daily_rate,
    )


def _parse_market(raw: dict) -> MarketInfo | None:
    condition_id = raw.get("condition_id") or raw.get("conditionId")
    if not condition_id:
        return None

    tokens_raw = raw.get("tokens") or []
    tokens = []
    for t in tokens_raw:
        token_id = t.get("token_id") or t.get("tokenId")
        outcome = t.get("outcome", "")
        if token_id:
            tokens.append(TokenInfo(token_id=str(token_id), outcome=str(outcome)))

    def _num(*keys, default=0.0):
        for k in keys:
            if raw.get(k) is not None:
                try:
                    return float(raw[k])
                except (TypeError, ValueError):
                    pass
        return default

    accepting = _first(raw, "accepting_orders", "acceptingOrders")
    neg_risk_id = _first(raw, "neg_risk_market_id", "negRiskMarketID")
    return MarketInfo(
        condition_id=str(condition_id),
        question=str(raw.get("question", "")),
        tokens=tokens,
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
        volume_usd=_num("volume", "volume24hr", "volumeNum"),
        liquidity_usd=_num("liquidity", "liquidityNum"),
        tags=_parse_tags(raw),
        end_date=_parse_time(_first(raw, "end_date_iso", "endDate", "end_date")),
        game_start_time=_parse_time(_first(raw, "game_start_time", "gameStartTime")),
        accepting_orders=True if accepting is None else bool(accepting),
        seconds_delay=_num("seconds_delay", "secondsDelay"),
        min_order_size=_float_or_none(_first(raw, "minimum_order_size", "min_order_size", "orderMinSize")),
        tick_size=_float_or_none(_first(raw, "minimum_tick_size", "orderPriceMinTickSize")),
        neg_risk=bool(_first(raw, "neg_risk", "negRisk")),
        neg_risk_market_id=str(neg_risk_id) if neg_risk_id else None,
        rewards=_parse_rewards(raw),
    )


def _parse_tags(raw: dict) -> list[str]:
    """Collect category labels from a market payload.

    Tags arrive either as plain strings or as objects with a label/slug,
    depending on the endpoint; a top-level "category" string is included too.
    Anything unrecognized is ignored (the fee model then uses its
    conservative default rate).
    """
    tags: list[str] = []
    for t in raw.get("tags") or []:
        if isinstance(t, str):
            tags.append(t)
        elif isinstance(t, dict):
            for key in ("label", "slug", "name"):
                if isinstance(t.get(key), str):
                    tags.append(t[key])
    if isinstance(raw.get("category"), str):
        tags.append(raw["category"])
    return [t for t in (s.strip() for s in tags) if t]


def iter_active_markets(client, cfg: MarketFilterConfig, limit: int | None = None):
    """Yield tradable MarketInfo objects, paginating through sampling markets.

    Applies the whitelist / volume / liquidity filters from config. Stops once
    `limit` (default: max_markets_per_cycle) markets have been yielded, to
    bound API usage.
    """
    if cfg.whitelist:
        whitelist = set(cfg.whitelist)
    else:
        whitelist = None
    limit = cfg.max_markets_per_cycle if limit is None else limit

    yielded = 0
    cursor = "MA=="
    seen_cursors = set()

    while yielded < limit:
        if cursor in seen_cursors:
            break  # API looped back; avoid infinite loop
        seen_cursors.add(cursor)

        try:
            resp = client.get_sampling_markets(next_cursor=cursor)
        except Exception:
            logger.exception("Failed to fetch sampling markets (cursor=%s)", cursor)
            break

        data = resp.get("data", []) if isinstance(resp, dict) else []
        if not data:
            break

        for raw in data:
            market = _parse_market(raw)
            if market is None:
                continue
            if not market.tokens or len(market.tokens) < 2:
                continue
            if not market.active or market.closed or not market.accepting_orders:
                continue
            if whitelist is not None and market.condition_id not in whitelist:
                continue
            if market.volume_usd and market.volume_usd < cfg.min_volume_usd:
                continue
            if market.liquidity_usd and market.liquidity_usd < cfg.min_liquidity_usd:
                continue

            yield market
            yielded += 1
            if yielded >= limit:
                break

        next_cursor = resp.get("next_cursor") if isinstance(resp, dict) else None
        if not next_cursor or next_cursor == cursor or next_cursor == "LTE=":
            break
        cursor = next_cursor


def parse_book(raw, token_id: str | None = None) -> OrderBook:
    """An OrderBook from a REST `/book` response (dict in the V2 SDK, object in V1)."""
    return OrderBook.from_snapshot(raw, token_id=token_id)


def best_levels(raw_book) -> BookLevel:
    """Best bid/ask (price + size) from a raw order-book response.

    Levels aren't guaranteed to arrive sorted, so the best ones are picked
    explicitly rather than taken from the front of the list.
    """
    return parse_book(raw_book).top()
