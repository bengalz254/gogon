"""Backup migration discovery: GeckoTerminal's public "new pools" list.

A pump.fun migration creates a PumpSwap pool, which GeckoTerminal lists
within about a minute. Polling it catches migrations the WebSocket missed
(for example while it was reconnecting). Free API, ~30 requests/minute.
"""
from __future__ import annotations

import logging
import time

from migbot.config import SOL_MINT
from migbot.http import JsonHttp
from migbot.models import MigrationEvent
from migbot.parsing import integer, iso_to_ts, sub, text
from migbot.solana import is_pubkey

logger = logging.getLogger("migbot.geckoterminal")


def _rel_id(pool: dict, name: str) -> str:
    data = sub(sub(sub(pool, "relationships"), name), "data")
    return text(data, "id")


def _address(gecko_id: str) -> str:
    # ids look like "solana_<address>"
    return gecko_id.split("_", 1)[1] if "_" in gecko_id else gecko_id


def parse_new_pools(payload, dexes: list[str], mint_suffixes: list[str], received_at: float) -> list[MigrationEvent]:
    if not isinstance(payload, dict):
        return []
    tokens = {}
    for item in payload.get("included") or []:
        if isinstance(item, dict) and item.get("type") == "token":
            tokens[text(item, "id")] = sub(item, "attributes")
    wanted = {d.lower() for d in dexes}
    events = []
    for pool in payload.get("data") or []:
        if not isinstance(pool, dict):
            continue
        attrs = sub(pool, "attributes")
        dex = _rel_id(pool, "dex")
        if wanted and dex.lower() not in wanted:
            continue
        base_id, quote_id = _rel_id(pool, "base_token"), _rel_id(pool, "quote_token")
        base, quote = _address(base_id), _address(quote_id)
        if base == SOL_MINT and quote != SOL_MINT:
            base, quote, base_id = quote, base, quote_id  # listed the other way round
        if quote != SOL_MINT or not is_pubkey(base):
            continue
        if mint_suffixes and not any(base.endswith(s) for s in mint_suffixes):
            continue
        created = iso_to_ts(text(attrs, "pool_created_at")) or received_at
        token = tokens.get(base_id, {})
        name = text(attrs, "name")
        events.append(
            MigrationEvent(
                mint=base,
                source="geckoterminal",
                migrated_at=min(created, received_at),
                received_at=received_at,
                pool=text(attrs, "address"),
                dex=dex,
                symbol=text(token, "symbol") or (name.split("/")[0].strip() if "/" in name else ""),
                name=text(token, "name"),
                decimals=integer(token, "decimals"),
            )
        )
    return events


class GeckoTerminalSource:
    def __init__(self, base_url: str, dexes: list[str], mint_suffixes: list[str], http: JsonHttp | None = None):
        self.base_url = base_url.rstrip("/")
        self.dexes = dexes
        self.mint_suffixes = mint_suffixes
        self.http = http or JsonHttp("GeckoTerminal", min_interval=2.5, retries=2)

    def poll(self) -> list[MigrationEvent]:
        payload = self.http.get_json(
            f"{self.base_url}/networks/solana/new_pools",
            params={"page": 1, "include": "base_token,quote_token"},
            headers={"Accept": "application/json;version=20230302"},
        )
        return parse_new_pools(payload, self.dexes, self.mint_suffixes, time.time())
