"""Window discovery via Polymarket's Gamma API.

Up/Down windows have deterministic slugs, e.g. `btc-updown-5m-1760000100`
(asset, interval, unix start time), so the bot can compute the next slugs
itself and look them up before each window opens, instead of scanning every
market on the site.

Gamma payloads have some field-name variance (JSON-encoded lists vs real
lists, camelCase vs snake_case), so parsing here is defensive.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime

import requests

from bot.updown.window import DOWN, UP, WindowSpec

logger = logging.getLogger("polybot.updown.discovery")


def slot_starts(now: float, interval_s: int, ahead: int) -> list[int]:
    """Start times of the current window and the next `ahead` windows."""
    current = int(math.floor(now / interval_s) * interval_s)
    return [current + k * interval_s for k in range(ahead + 1)]


def build_slug(template: str, asset: str, interval_label: str, start: int) -> str:
    return template.format(asset=asset.lower(), interval=interval_label.lower(), start=int(start))


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return [v.strip() for v in value.split(",") if v.strip()]
    return []


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_market(payload: dict) -> dict | None:
    markets = payload.get("markets")
    if isinstance(markets, list) and markets:
        return markets[0]
    if payload.get("clobTokenIds") or payload.get("conditionId"):
        return payload  # already a market
    return None


def parse_window(payload: dict, *, asset: str, interval_s: int, start: int, slug: str) -> WindowSpec | None:
    """Build a WindowSpec from a Gamma event (or market) payload."""
    market = _first_market(payload)
    if market is None:
        return None
    outcomes = [str(o).strip().lower() for o in _as_list(market.get("outcomes"))]
    tokens = [str(t) for t in _as_list(market.get("clobTokenIds") or market.get("clob_token_ids"))]
    if len(outcomes) != 2 or len(tokens) != 2:
        logger.warning("%s: unexpected outcomes/tokens %s / %s", slug, outcomes, tokens)
        return None
    try:
        up_i, down_i = outcomes.index(UP.lower()), outcomes.index(DOWN.lower())
    except ValueError:
        logger.warning("%s: outcomes are %s, not Up/Down; skipping", slug, outcomes)
        return None
    condition_id = market.get("conditionId") or market.get("condition_id") or ""
    if not condition_id:
        return None
    return WindowSpec(
        window_id=slug,
        asset=asset.lower(),
        interval_s=interval_s,
        start=float(start),
        end=float(start + interval_s),
        condition_id=str(condition_id),
        up_token=tokens[up_i],
        down_token=tokens[down_i],
        tick_size=_float(market.get("orderPriceMinTickSize"), 0.01) or 0.01,
        min_order_size=_float(market.get("orderMinSize"), 5.0) or 5.0,
        neg_risk=bool(market.get("negRisk") or payload.get("negRisk") or False),
        question=str(market.get("question") or payload.get("title") or slug),
    )


def parse_resolution(payload: dict) -> str | None:
    """'Up' / 'Down' once Gamma shows the market resolved, else None."""
    market = _first_market(payload)
    if market is None:
        return None
    outcomes = [str(o).strip() for o in _as_list(market.get("outcomes"))]
    prices = [_float(p, 0.0) for p in _as_list(market.get("outcomePrices"))]
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    resolved = market.get("closed") is True or str(market.get("umaResolutionStatus", "")).lower() == "resolved"
    if not resolved:
        return None
    for name, price in zip(outcomes, prices):
        if price >= 0.99:
            for canonical in (UP, DOWN):
                if name.lower() == canonical.lower():
                    return canonical
    return None


_PTB_KEYS = ("priceToBeat", "price_to_beat", "startPrice", "openPrice")


def parse_price_to_beat(payload: dict) -> float | None:
    """Official opening price if Gamma exposes one (field names vary)."""
    candidates = [payload, payload.get("eventMetadata") or {}]
    market = _first_market(payload)
    if market is not None and market is not payload:
        candidates += [market, market.get("eventMetadata") or {}]
    for obj in candidates:
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except ValueError:
                continue
        if not isinstance(obj, dict):
            continue
        for key in _PTB_KEYS:
            value = _float(obj.get(key))
            if value and value > 0:
                return value
    return None


def parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class GammaClient:
    def __init__(self, base_url: str, timeout: float = 6.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: dict) -> object:
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def event_by_slug(self, slug: str) -> dict | None:
        data = self._get("/events", {"slug": slug})
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict) and data.get("slug") == slug:
            return data
        return None

    def market_by_slug(self, slug: str) -> dict | None:
        data = self._get("/markets", {"slug": slug})
        if isinstance(data, list):
            return data[0] if data else None
        return data if isinstance(data, dict) and data else None

    def lookup(self, slug: str) -> dict | None:
        """Event payload for a slug, falling back to the market endpoint."""
        event = self.event_by_slug(slug)
        if event:
            return event
        return self.market_by_slug(slug)


def discover_window(gamma: GammaClient, *, slug: str, asset: str, interval_s: int, start: int) -> WindowSpec | None:
    payload = gamma.lookup(slug)
    if not payload:
        return None
    return parse_window(payload, asset=asset, interval_s=interval_s, start=start, slug=slug)
