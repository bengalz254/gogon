"""Shared JSON-over-HTTP helper: timeouts, a little retrying, per-source
throttling, and a health record the dashboard shows for every data source.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from migbot import __version__

logger = logging.getLogger("migbot.http")

USER_AGENT = f"migbot/{__version__} (+paper-trading research bot)"


_SECRETS: set[str] = set()
_KEY_PARAM = re.compile(r"(?i)((?:api[-_]?key|access[-_]?token|token|secret|key)=)[^&\s'\"<>)]+")
_BOT_PATH = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
_BOT_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")


def register_secret(value: str | None) -> None:
    """Never print this value (a token, an API key, a private RPC URL) in logs or messages."""
    if not value or len(value) < 8:
        return
    _SECRETS.add(value)
    parts = urlsplit(value) if "://" in value else None
    if parts is not None:
        if parts.query:
            _SECRETS.add(parts.query)
        last = parts.path.rstrip("/").rsplit("/", 1)[-1]
        if len(last) >= 16:  # providers that put the key in the path (…/v2/<key>)
            _SECRETS.add(last)


def redact(text) -> str:
    """The text with tokens, API keys and registered secrets replaced by ***."""
    text = str(text)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "***")
    text = _KEY_PARAM.sub(r"\1***", text)
    text = _BOT_PATH.sub("/bot***", text)
    return _BOT_TOKEN.sub("***", text)


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, blocked: bool = False):
        super().__init__(redact(message))
        self.status = status
        self.blocked = blocked


@dataclass
class Health:
    name: str
    ok_count: int = 0
    error_count: int = 0
    consecutive_errors: int = 0
    last_ok: float = 0.0
    last_error_at: float = 0.0
    last_error: str = ""
    note: str = ""

    def ok(self) -> None:
        self.ok_count += 1
        self.consecutive_errors = 0
        self.last_ok = time.time()

    def fail(self, message: str) -> None:
        self.error_count += 1
        self.consecutive_errors += 1
        self.last_error_at = time.time()
        self.last_error = message[:300]

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "ok_count": self.ok_count,
            "error_count": self.error_count,
            "consecutive_errors": self.consecutive_errors,
            "last_ok": self.last_ok,
            "last_error_at": self.last_error_at,
            "last_error": self.last_error,
            "note": self.note,
        }


def _looks_blocked(resp: requests.Response) -> bool:
    ctype = resp.headers.get("Content-Type", "")
    body = resp.text[:2000].lower() if "html" in ctype else ""
    return resp.status_code in (401, 403) or "just a moment" in body or "cloudflare" in body


class JsonHttp:
    """GET/POST returning parsed JSON, or raising HttpError with a short reason."""

    def __init__(
        self,
        name: str,
        min_interval: float = 0.0,
        timeout: float = 10.0,
        retries: int = 2,
        headers: dict | None = None,
        session: requests.Session | None = None,
    ):
        self.health = Health(name)
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = max(1, retries)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if headers:
            self.session.headers.update(headers)
        self._lock = threading.Lock()
        self._last = 0.0

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def request(self, method: str, url: str, **kwargs):
        last: HttpError | None = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                last = HttpError(f"{type(exc).__name__}: {exc}")
                if attempt < self.retries:
                    time.sleep(attempt)
                continue
            if _looks_blocked(resp):
                err = HttpError(f"HTTP {resp.status_code}: diblokir (Cloudflare/izin)", resp.status_code, blocked=True)
                self.health.fail(str(err))
                raise err
            if resp.status_code == 429 or resp.status_code >= 500:
                last = HttpError(f"HTTP {resp.status_code}", resp.status_code)
                if attempt < self.retries:
                    retry_after = resp.headers.get("Retry-After", "")
                    time.sleep(min(float(retry_after), 10.0) if retry_after.isdigit() else 2.0 * attempt)
                continue
            if resp.status_code >= 400:
                err = HttpError(f"HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)
                self.health.fail(str(err))
                raise err
            try:
                data = resp.json()
            except ValueError:
                err = HttpError(f"bukan JSON: {resp.text[:120]!r}", resp.status_code)
                self.health.fail(str(err))
                raise err
            self.health.ok()
            return data
        assert last is not None
        self.health.fail(str(last))
        raise last

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        return self.request("GET", url, params=params, headers=headers)

    def post_json(self, url: str, payload, headers: dict | None = None):
        return self.request("POST", url, json=payload, headers=headers)
