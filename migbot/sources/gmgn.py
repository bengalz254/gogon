"""Optional extra data from gmgn.ai: holder count, smart-money buys, snipers.

gmgn.ai has no official public API. This calls the JSON endpoint its own
token page uses, lightly (one request per candidate token), with a plain
HTTP client and no attempt to get around Cloudflare. When GMGN blocks the
server (403 / challenge page) the bot pauses GMGN for a while and keeps
working with its other sources; GMGN-only filters are then skipped.
"""
from __future__ import annotations

import time

from migbot.http import HttpError, JsonHttp
from migbot.models import GmgnInfo
from migbot.parsing import integer, num, sub, text


def token_url(mint: str) -> str:
    return f"https://gmgn.ai/sol/token/{mint}"


def parse_token_info(payload) -> GmgnInfo:
    if not isinstance(payload, dict):
        raise ValueError("jawaban GMGN tidak dikenal")
    if payload.get("code") not in (0, None):
        raise ValueError(f"GMGN error: {payload.get('msg') or payload.get('code')}")
    data = sub(payload, "data")
    token = sub(data, "token") or data
    if not token:
        raise ValueError("GMGN tidak mengirim data token")
    top10 = num(token, "top_10_holder_rate", "top10_holder_rate")
    if top10 is not None and top10 <= 1.0:
        top10 *= 100.0
    return GmgnInfo(
        ts=time.time(),
        holders=integer(token, "holder_count", "holders"),
        top10_pct=top10,
        smart_buys=integer(token, "smart_buy_24h", "smart_buy"),
        smart_sells=integer(token, "smart_sell_24h", "smart_sell"),
        sniper_count=integer(token, "sniper_count", "snipers"),
        dev_status=text(token, "creator_token_status", "dev_token_status"),
    )


class GmgnSource:
    def __init__(self, base_url: str, blocked_retry_minutes: float = 30.0, http: JsonHttp | None = None):
        self.base_url = base_url.rstrip("/")
        self.blocked_retry = blocked_retry_minutes * 60
        self.http = http or JsonHttp("GMGN", min_interval=2.0, retries=1)
        self.blocked_until = 0.0

    @property
    def available(self) -> bool:
        return time.time() >= self.blocked_until

    def fetch(self, mint: str) -> GmgnInfo:
        try:
            payload = self.http.get_json(f"{self.base_url}/defi/quotation/v1/tokens/sol/{mint}")
        except HttpError as exc:
            if exc.blocked:
                self.blocked_until = time.time() + self.blocked_retry
                self.http.health.note = "diblokir oleh GMGN; filter khusus GMGN dilewati"
            raise
        try:
            info = parse_token_info(payload)
        except ValueError as exc:
            self.http.health.fail(str(exc))
            raise
        self.http.health.note = ""
        return info
