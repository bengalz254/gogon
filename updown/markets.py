"""Finding the current 5-minute market on Polymarket and reading its order books.

Gamma (market metadata) and CLOB (order books) are both public, read-only
endpoints, so this needs no wallet. Field names are parsed defensively:
several of them come back as JSON-encoded strings rather than lists.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests

from updown.strategy import DOWN, UP, Book

logger = logging.getLogger("polybot.updown.markets")


@dataclass
class WindowMarket:
    asset: str
    start: int  # unix seconds, window open
    end: int  # unix seconds, window close
    slug: str
    condition_id: str
    tokens: dict[str, str]  # UP/DOWN -> CLOB token id
    price_to_beat: float | None = None  # from Polymarket, when it publishes one


def window_start(ts: float, window_seconds: int) -> int:
    return int(ts // window_seconds * window_seconds)


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            return json.loads(value)
        except ValueError:
            return []
    return []


def _find_price_to_beat(*objs: dict) -> float | None:
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for container in (obj, obj.get("eventMetadata") or {}):
            if isinstance(container, str):
                try:
                    container = json.loads(container)
                except ValueError:
                    continue
            for key in ("priceToBeat", "price_to_beat"):
                val = container.get(key) if isinstance(container, dict) else None
                try:
                    if val is not None and float(val) > 0:
                        return float(val)
                except (TypeError, ValueError):
                    pass
    return None


def parse_market(asset: str, start: int, window_seconds: int, slug: str, market: dict, event: dict | None = None) -> WindowMarket | None:
    outcomes = [str(o) for o in _as_list(market.get("outcomes"))]
    token_ids = [str(t) for t in _as_list(market.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(token_ids) != 2:
        logger.warning("Market %s: unexpected outcomes/tokens %s / %s", slug, outcomes, token_ids)
        return None
    tokens = {}
    for name, tid in zip(outcomes, token_ids):
        key = {"up": UP, "down": DOWN, "yes": UP, "no": DOWN}.get(name.strip().lower())
        if key:
            tokens[key] = tid
    if set(tokens) != {UP, DOWN}:
        logger.warning("Market %s: can't map outcomes %s to Up/Down", slug, outcomes)
        return None
    return WindowMarket(
        asset=asset,
        start=start,
        end=start + window_seconds,
        slug=slug,
        condition_id=str(market.get("conditionId") or market.get("condition_id") or slug),
        tokens=tokens,
        price_to_beat=_find_price_to_beat(market, event or {}),
    )


def parse_resolution(market: dict) -> str | None:
    """UP/DOWN once the market has settled, else None."""
    if not market.get("closed"):
        return None
    outcomes = [str(o).strip().lower() for o in _as_list(market.get("outcomes"))]
    prices = _as_list(market.get("outcomePrices"))
    for name, price in zip(outcomes, prices):
        try:
            if float(price) >= 0.99:
                return {"up": UP, "down": DOWN, "yes": UP, "no": DOWN}.get(name)
        except (TypeError, ValueError):
            continue
    return None


def parse_book(raw: dict) -> Book:
    def levels(rows):
        out = []
        for r in rows or []:
            try:
                out.append((float(r["price"]), float(r["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    return Book.from_raw(levels(raw.get("bids")), levels(raw.get("asks")))


class PolymarketGateway:
    def __init__(self, gamma_host: str, clob_host: str, slug_template: str, window_seconds: int):
        self.gamma_host = gamma_host.rstrip("/")
        self.clob_host = clob_host.rstrip("/")
        self.slug_template = slug_template
        self.window_seconds = window_seconds
        self.session = requests.Session()

    def _get(self, url: str, **params):
        resp = self.session.get(url, params=params, timeout=4)
        resp.raise_for_status()
        return resp.json()

    def _fetch_market(self, slug: str) -> tuple[dict | None, dict | None]:
        events = self._get(f"{self.gamma_host}/events", slug=slug)
        if events:
            event = events[0]
            markets = event.get("markets") or []
            if markets:
                return markets[0], event
        markets = self._get(f"{self.gamma_host}/markets", slug=slug)
        return (markets[0], None) if markets else (None, None)

    def discover(self, asset: str, start: int) -> WindowMarket | None:
        slug = self.slug_template.format(asset=asset, start=start)
        market, event = self._fetch_market(slug)
        if market is None:
            return None
        return parse_market(asset, start, self.window_seconds, slug, market, event)

    def refresh_price_to_beat(self, wm: WindowMarket) -> float | None:
        market, event = self._fetch_market(wm.slug)
        return _find_price_to_beat(market or {}, event or {})

    def book(self, token_id: str) -> Book:
        return parse_book(self._get(f"{self.clob_host}/book", token_id=token_id))

    def books(self, token_ids: list[str]) -> dict[str, Book]:
        """All the books in one request (POST /books): 7 coins x 2 sides every
        second would otherwise be 14 requests a second."""
        resp = self.session.post(f"{self.clob_host}/books", json=[{"token_id": t} for t in token_ids], timeout=4)
        resp.raise_for_status()
        out = {}
        for raw in resp.json() or []:
            tid = str(raw.get("asset_id") or raw.get("token_id") or "")
            if tid:
                out[tid] = parse_book(raw)
        return out

    def resolution(self, wm: WindowMarket) -> str | None:
        market, _ = self._fetch_market(wm.slug)
        return parse_resolution(market) if market else None
