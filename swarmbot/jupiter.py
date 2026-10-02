"""Jupiter Tokens API v2: the same data dotswarm.fun reads its moods from."""
from __future__ import annotations

import logging
import time

import requests

from swarmbot import __version__

log = logging.getLogger("swarmbot.jupiter")


class JupiterError(RuntimeError):
    pass


class Jupiter:
    def __init__(self, base_url: str, api_key: str = "", session=None, min_interval: float = 1.1):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = f"swarmbot/{__version__}"
        if api_key:
            self.session.headers["x-api-key"] = api_key
        self.min_interval = min_interval
        self._last = 0.0

    def _get(self, path: str, params: dict | None = None):
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = f"{self.base_url}/tokens/v2/{path.lstrip('/')}"
        error = ""
        for attempt in range(3):
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=15)
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        error = "jawaban bukan JSON"
                else:
                    error = f"HTTP {resp.status_code}"
                    if resp.status_code in (401, 403):
                        break
            time.sleep(2 * (attempt + 1))
        raise JupiterError(f"Jupiter {path}: {error}")

    def token_list(self, name: str, limit: int = 100) -> list[dict]:
        """name like 'toptrending/1h' or 'recent'."""
        data = self._get(name, {"limit": str(limit)} if name != "recent" else None)
        return data if isinstance(data, list) else []

    def tokens(self, mints: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(mints), 100):
            data = self._get("search", {"query": ",".join(mints[i:i + 100])})
            if isinstance(data, list):
                out.extend(data)
        return out
