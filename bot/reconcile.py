"""Compare the bot's own position tracking with what Polymarket reports.

The bot only knows what it believes it filled. Partial fills, fees taken in
shares, manual trades in the UI, redemptions after a market resolves, or a
lost state file all make that belief drift from reality. In live mode this
module fetches the wallet's actual positions from Polymarket's public Data
API and reports every mismatch. It never changes the bot's state on its own —
a human decides what the right fix is.

NOTE: written against the Data API's documented shape but not yet tested
against the live API. Treat a "could not reconcile" warning as a reason to
look, not as proof that positions match.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger("polybot.reconcile")

DATA_API_POSITIONS_URL = "https://data-api.polymarket.com/positions"


def fetch_exchange_positions(address: str, timeout: float = 10.0) -> dict[str, float]:
    """token_id -> shares held by `address` (the funder/proxy wallet) on Polymarket."""
    resp = requests.get(DATA_API_POSITIONS_URL, params={"user": address, "limit": 500}, timeout=timeout)
    resp.raise_for_status()
    return parse_positions(resp.json())


def parse_positions(payload) -> dict[str, float]:
    """Parse a Data API /positions response defensively (unknown rows are skipped)."""
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("positions") or []
    if not isinstance(payload, list):
        return {}

    positions: dict[str, float] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        # Resolved and waiting to be redeemed: the bot has already settled
        # these in its own books (bot/settlement.py).
        if row.get("redeemable") is True:
            continue
        token_id = row.get("asset") or row.get("asset_id") or row.get("token_id") or row.get("tokenId")
        try:
            size = float(row.get("size", 0))
        except (TypeError, ValueError):
            continue
        if token_id and size > 0:
            positions[str(token_id)] = positions.get(str(token_id), 0.0) + size
    return positions


def diff_positions(local: dict[str, float], remote: dict[str, float]) -> list[str]:
    """Human-readable mismatches between the bot's positions and the exchange's.

    Small differences are tolerated (up to 1 share or 10%): taker fees taken
    in shares and the Data API hiding dust positions both cause them, and
    this check is meant to catch real drift — missing, unknown, or clearly
    wrong positions.
    """
    mismatches = []
    for token_id in sorted(set(local) | set(remote)):
        mine, theirs = local.get(token_id, 0.0), remote.get(token_id, 0.0)
        if abs(mine - theirs) > max(1.0, 0.10 * max(mine, theirs)):
            mismatches.append(f"token {token_id[:16]}…: bot {mine:.2f} vs exchange {theirs:.2f} shares")
    return mismatches


def reconcile(address: str, local: dict[str, float]) -> list[str] | None:
    """Mismatches between `local` and the exchange, or None if the check couldn't run."""
    try:
        remote = fetch_exchange_positions(address)
    except Exception as exc:
        logger.warning("Could not reconcile positions with Polymarket (%s)", type(exc).__name__)
        return None
    return diff_positions(local, remote)
