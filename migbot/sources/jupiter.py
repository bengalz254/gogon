"""Swap quotes from Jupiter, used to price paper fills realistically: the
quote includes the pool fee and the price impact of the actual size.
"""
from __future__ import annotations

from migbot.http import HttpError, JsonHttp
from migbot.models import Quote
from migbot.parsing import num, sub, text


class NoRoute(RuntimeError):
    pass


def parse_quote(payload) -> Quote:
    if not isinstance(payload, dict):
        raise NoRoute("jawaban Jupiter tidak dikenal")
    if payload.get("error") or payload.get("errorCode"):
        raise NoRoute(str(payload.get("error") or payload.get("errorCode")))
    try:
        in_amount = int(payload["inAmount"])
        out_amount = int(payload["outAmount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise NoRoute("quote tanpa jumlah") from exc
    if out_amount <= 0:
        raise NoRoute("quote kosong")
    labels = []
    for step in payload.get("routePlan") or []:
        label = text(sub(step, "swapInfo"), "label")
        if label and label not in labels:
            labels.append(label)
    impact = num(payload, "priceImpactPct", default=0.0) or 0.0
    return Quote(in_amount=in_amount, out_amount=out_amount, price_impact_pct=impact * 100, route=" > ".join(labels))


class JupiterSource:
    def __init__(self, base_url: str, api_key: str = "", http: JsonHttp | None = None):
        self.base_url = base_url.rstrip("/")
        headers = {"x-api-key": api_key} if api_key else None
        self.http = http or JsonHttp("Jupiter", min_interval=1.1, retries=2, headers=headers)

    def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 500) -> Quote:
        try:
            payload = self.http.get_json(
                f"{self.base_url}/quote",
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": str(int(amount)),
                    "slippageBps": str(slippage_bps),
                    "restrictIntermediateTokens": "true",
                },
            )
        except HttpError as exc:
            if exc.status == 400:
                raise NoRoute(str(exc)) from exc
            raise
        return parse_quote(payload)
