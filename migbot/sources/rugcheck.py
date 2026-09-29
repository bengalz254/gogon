"""Risk summary from RugCheck's public API (api.rugcheck.xyz)."""
from __future__ import annotations

import time

from migbot.http import JsonHttp
from migbot.models import RugcheckReport
from migbot.parsing import num, text


def parse_summary(payload) -> RugcheckReport:
    if not isinstance(payload, dict):
        raise ValueError("jawaban RugCheck tidak dikenal")
    risks = []
    for risk in payload.get("risks") or []:
        if isinstance(risk, dict) and text(risk, "name"):
            risks.append(
                {
                    "name": text(risk, "name"),
                    "level": text(risk, "level").lower(),
                    "description": text(risk, "description")[:160],
                }
            )
    return RugcheckReport(
        ts=time.time(),
        score=num(payload, "score"),
        score_normalised=num(payload, "score_normalised", "scoreNormalised"),
        risks=risks,
    )


class RugcheckSource:
    def __init__(self, base_url: str, http: JsonHttp | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or JsonHttp("RugCheck", min_interval=1.0, retries=2)

    def fetch(self, mint: str) -> RugcheckReport:
        return parse_summary(self.http.get_json(f"{self.base_url}/tokens/{mint}/report/summary"))
