"""Prices, liquidity, volume and buy/sell counts from DexScreener's public API.

`GET /tokens/v1/solana/{mint,mint,...}` takes up to 30 tokens per request
(limit 300 requests/minute), so every tracked token is refreshed with one or
two requests per cycle.
"""
from __future__ import annotations

import logging
import time

from migbot.config import SOL_MINT
from migbot.http import JsonHttp
from migbot.models import MarketSnapshot
from migbot.parsing import integer, ms_or_s_to_ts, num, sub, text

logger = logging.getLogger("migbot.dexscreener")

BATCH = 30


def parse_pairs(payload) -> list[dict]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("pairs"), list):
        return [p for p in payload["pairs"] if isinstance(p, dict)]
    return []


def _socials(pair: dict) -> list[str]:
    info = sub(pair, "info")
    out = []
    for site in info.get("websites") or []:
        url = text(site, "url") if isinstance(site, dict) else ""
        if url:
            out.append(url)
    for social in info.get("socials") or []:
        if not isinstance(social, dict):
            continue
        kind = text(social, "type", "platform")
        target = text(social, "url", "handle")
        if target:
            out.append(f"{kind}:{target}" if kind and not target.startswith("http") else target)
    return out


def snapshot_from_pair(pair: dict, now: float) -> MarketSnapshot:
    txns, vol, change = sub(pair, "txns"), sub(pair, "volume"), sub(pair, "priceChange")
    base = sub(pair, "baseToken")
    return MarketSnapshot(
        ts=now,
        pair_address=text(pair, "pairAddress"),
        dex_id=text(pair, "dexId"),
        price_usd=num(pair, "priceUsd"),
        price_native=num(pair, "priceNative"),
        liquidity_usd=num(sub(pair, "liquidity"), "usd"),
        market_cap_usd=num(pair, "marketCap", "fdv"),
        volume_m5=num(vol, "m5"),
        volume_h1=num(vol, "h1"),
        buys_m5=integer(sub(txns, "m5"), "buys"),
        sells_m5=integer(sub(txns, "m5"), "sells"),
        buys_h1=integer(sub(txns, "h1"), "buys"),
        sells_h1=integer(sub(txns, "h1"), "sells"),
        change_m5=num(change, "m5"),
        change_h1=num(change, "h1"),
        pair_created_at=ms_or_s_to_ts(pair.get("pairCreatedAt")),
        symbol=text(base, "symbol"),
        name=text(base, "name"),
        socials=_socials(pair),
        url=text(pair, "url"),
    )


def best_pair(pairs: list[dict], mint: str, known_pair: str = "") -> dict | None:
    """The token's SOL pair: the one already followed, else the most liquid."""
    candidates = [
        p
        for p in pairs
        if text(sub(p, "baseToken"), "address") == mint and text(sub(p, "quoteToken"), "address") == SOL_MINT
    ]
    if not candidates:
        return None
    if known_pair:
        for p in candidates:
            if text(p, "pairAddress") == known_pair:
                return p
    return max(candidates, key=lambda p: num(sub(p, "liquidity"), "usd", default=0.0) or 0.0)


class DexScreenerSource:
    def __init__(self, base_url: str, http: JsonHttp | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or JsonHttp("DexScreener", min_interval=0.25, retries=2)

    def fetch(self, mints: list[str], known_pairs: dict[str, str] | None = None) -> dict[str, MarketSnapshot]:
        known_pairs = known_pairs or {}
        out: dict[str, MarketSnapshot] = {}
        error: Exception | None = None
        succeeded = 0
        for i in range(0, len(mints), BATCH):
            chunk = mints[i : i + BATCH]
            try:
                payload = self.http.get_json(f"{self.base_url}/tokens/v1/solana/{','.join(chunk)}")
            except Exception as exc:  # noqa: BLE001 - other chunks may still succeed
                error = exc
                continue
            succeeded += 1
            pairs = parse_pairs(payload)
            now = time.time()
            for mint in chunk:
                pair = best_pair(pairs, mint, known_pairs.get(mint, ""))
                if pair is not None:
                    out[mint] = snapshot_from_pair(pair, now)
        if error is not None and not succeeded:
            raise error
        return out
