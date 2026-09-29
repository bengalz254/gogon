"""Defensive field readers: third-party payloads change shape without notice,
so a missing or malformed field becomes None instead of a crash.
"""
from __future__ import annotations

import math
from datetime import datetime


def num(raw, *keys, default=None) -> float | None:
    if not isinstance(raw, dict):
        return default
    for key in keys:
        value = raw.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            out = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(out):
            return out
    return default


def integer(raw, *keys, default=None) -> int | None:
    value = num(raw, *keys)
    return int(value) if value is not None else default


def text(raw, *keys, default: str = "") -> str:
    if not isinstance(raw, dict):
        return default
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def sub(raw, key) -> dict:
    value = raw.get(key) if isinstance(raw, dict) else None
    return value if isinstance(value, dict) else {}


def iso_to_ts(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ms_or_s_to_ts(value) -> float | None:
    """Timestamps arrive in seconds or milliseconds depending on the source."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts) or ts <= 0:
        return None
    return ts / 1000.0 if ts > 1e11 else ts
