"""Market discovery and order-book helpers.

Polymarket's public market-listing payloads have some field-naming variance
across endpoints/versions, so parsing here is deliberately defensive: unknown
or missing fields fall back to safe defaults and are logged once rather than
crashing the bot.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from py_clob_client.clob_types import OrderBookSummary

from bot.config import MarketFilterConfig

logger = logging.getLogger("polybot.market_data")


@dataclass
class TokenInfo:
    token_id: str
    outcome: str


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


@dataclass
class BookLevel:
    """Best bid/ask summary for one token."""

    best_bid: float | None
    best_ask: float | None
    best_bid_size: float
    best_ask_size: float


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

    return MarketInfo(
        condition_id=str(condition_id),
        question=str(raw.get("question", "")),
        tokens=tokens,
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
        volume_usd=_num("volume", "volume24hr", "volumeNum"),
        liquidity_usd=_num("liquidity", "liquidityNum"),
        tags=_parse_tags(raw),
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


def iter_active_markets(client, cfg: MarketFilterConfig):
    """Yield tradable MarketInfo objects, paginating through sampling markets.

    Applies the whitelist / volume / liquidity filters from config. Stops once
    max_markets_per_cycle markets have been yielded, to bound API usage.
    """
    if cfg.whitelist:
        whitelist = set(cfg.whitelist)
    else:
        whitelist = None

    yielded = 0
    cursor = "MA=="
    seen_cursors = set()

    while yielded < cfg.max_markets_per_cycle:
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
            if not market.active or market.closed:
                continue
            if whitelist is not None and market.condition_id not in whitelist:
                continue
            if market.volume_usd and market.volume_usd < cfg.min_volume_usd:
                continue
            if market.liquidity_usd and market.liquidity_usd < cfg.min_liquidity_usd:
                continue

            yield market
            yielded += 1
            if yielded >= cfg.max_markets_per_cycle:
                break

        next_cursor = resp.get("next_cursor") if isinstance(resp, dict) else None
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor


def best_levels(book: OrderBookSummary) -> BookLevel:
    """Extract best bid/ask (price + size) from a raw order book summary.

    py-clob-client returns bids/asks as OrderSummary(price, size) strings,
    not guaranteed to be sorted, so we sort defensively.
    """
    bids = sorted(
        (b for b in (book.bids or []) if b.price is not None),
        key=lambda b: float(b.price),
        reverse=True,
    )
    asks = sorted(
        (a for a in (book.asks or []) if a.price is not None),
        key=lambda a: float(a.price),
    )

    best_bid = float(bids[0].price) if bids else None
    best_ask = float(asks[0].price) if asks else None
    best_bid_size = float(bids[0].size) if bids else 0.0
    best_ask_size = float(asks[0].size) if asks else 0.0

    return BookLevel(
        best_bid=best_bid,
        best_ask=best_ask,
        best_bid_size=best_bid_size,
        best_ask_size=best_ask_size,
    )
