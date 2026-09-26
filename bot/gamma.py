"""Multi-outcome ("negative risk") events from Polymarket's Gamma API.

In a negRisk event exactly one of N outcomes resolves YES, so holding one
YES share of *every* outcome always pays exactly $1 — the multi-outcome
version of a complete set. That only holds if the bot really holds every
outcome, so events are accepted only when that can be established
(fail closed — any doubt skips the event):

  - the event is negRisk;
  - it is known NOT to be "augmented": augmented events (enableNegRisk and
    negRiskAugmented both true) can add outcomes later, e.g. a surprise
    candidate, which would make "all current outcomes" no longer all;
  - it has no "Other" placeholder outcome;
  - every outcome market is open and trading, with a Yes/No token pair.

NOTE: written against the Gamma API's documented fields but not yet tested
against the live API.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field

import requests

from bot.market_data import MarketInfo, TokenInfo, _float_or_none, _parse_tags

logger = logging.getLogger("polybot.gamma")

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


@dataclass
class NegRiskEvent:
    event_id: str
    title: str
    # Groups the event's positions for exposure caps and complete sets.
    set_id: str
    # One binary market per outcome; tokens[0] is that outcome's YES token.
    outcomes: list[MarketInfo] = field(default_factory=list)


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def parse_negrisk_event(raw) -> tuple[NegRiskEvent | None, str]:
    """(event, "") when it's a safe multi-outcome set, else (None, why not)."""
    if not isinstance(raw, dict) or raw.get("negRisk") is not True:
        return None, "not a negRisk event"
    if not (raw.get("negRiskAugmented") is False or raw.get("enableNegRisk") is False):
        return None, "may add outcomes later (augmented, or not stated)"
    markets = raw.get("markets") or []
    if len(markets) < 2:
        return None, "fewer than two outcomes"

    event_id = str(raw.get("id") or "")
    set_id = str(raw.get("negRiskMarketID") or f"event:{event_id}")
    tags = _parse_tags(raw)
    outcomes = []
    for m in markets:
        if not isinstance(m, dict):
            return None, "malformed outcome"
        if m.get("negRiskOther") is True:
            return None, "has an 'Other' placeholder outcome"
        if (
            m.get("closed") is True
            or m.get("active") is False
            or m.get("acceptingOrders") is False
            or m.get("enableOrderBook") is False
        ):
            return None, "an outcome is closed or not trading"
        tokens = [str(t) for t in _json_list(m.get("clobTokenIds"))]
        names = [str(n).strip().lower() for n in _json_list(m.get("outcomes"))]
        if len(tokens) != 2 or names != ["yes", "no"]:
            return None, "an outcome lacks a Yes/No token pair"
        condition_id = m.get("conditionId")
        if not condition_id:
            return None, "an outcome lacks a condition id"
        outcomes.append(
            MarketInfo(
                condition_id=str(condition_id),
                question=str(m.get("groupItemTitle") or m.get("question") or ""),
                tokens=[TokenInfo(tokens[0], "Yes"), TokenInfo(tokens[1], "No")],
                tags=tags,
                seconds_delay=_float_or_none(m.get("secondsDelay")) or 0.0,
                min_order_size=_float_or_none(m.get("orderMinSize")),
                tick_size=_float_or_none(m.get("orderPriceMinTickSize")),
                neg_risk=True,
                neg_risk_market_id=set_id,
            )
        )
    if len({o.condition_id for o in outcomes}) != len(outcomes):
        return None, "duplicate outcomes"
    return NegRiskEvent(event_id=event_id, title=str(raw.get("title") or ""), set_id=set_id, outcomes=outcomes), ""


def fetch_negrisk_events(
    max_events: int = 50, max_outcomes: int = 10, page_size: int = 100, max_pages: int = 10, session=requests
) -> list[NegRiskEvent]:
    """Open negRisk events that pass parse_negrisk_event, up to `max_events`."""
    events: list[NegRiskEvent] = []
    skipped: Counter = Counter()
    for page in range(max_pages):
        resp = session.get(
            GAMMA_EVENTS_URL,
            params={"active": "true", "closed": "false", "limit": page_size, "offset": page * page_size},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload if isinstance(payload, list) else (payload.get("data") or payload.get("events") or [])
        for raw in rows:
            event, reason = parse_negrisk_event(raw)
            if event is None:
                if isinstance(raw, dict) and raw.get("negRisk") is True:
                    skipped[reason] += 1
            elif len(event.outcomes) > max_outcomes:
                skipped["too many outcomes"] += 1
            else:
                events.append(event)
            if len(events) >= max_events:
                break
        if len(rows) < page_size or len(events) >= max_events:
            break
    if skipped:
        logger.info("Skipped negRisk events: %s", dict(skipped))
    logger.info("Loaded %d multi-outcome events for arbitrage", len(events))
    return events
