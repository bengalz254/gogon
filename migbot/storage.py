"""CSV journals and the atomic JSON state/status files."""
from __future__ import annotations

import csv
import gzip
import json
import logging
import math
import os
import time

logger = logging.getLogger("migbot.storage")

TRADE_FIELDS = [
    "time_utc", "mint", "symbol", "side", "reason", "sol", "tokens", "price_native", "price_usd",
    "mcap_usd", "method", "impact_pct", "fee_sol", "pnl_sol", "pnl_pct", "position_pnl_sol", "balance_sol",
    # filled when a position closes: best/worst price vs the entry while held, minutes held, minutes after migration
    "peak_pct", "low_pct", "held_min", "entry_age_min",
]


class CsvJournal:
    """Append-only CSV. A file with a different header (older version or
    changed checkpoints) is renamed aside and a new one is started."""

    def __init__(self, path: str, fields: list[str]):
        self.path = path
        self.fields = fields
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as fh:
                header = next(csv.reader(fh), None)
            if header and header != fields:
                aside = f"{path[:-4]}.old-{time.strftime('%Y%m%d-%H%M%S')}.csv"
                os.replace(path, aside)
                logger.warning("%s had different columns; moved to %s", path, aside)

    def append(self, row: dict) -> None:
        new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        with open(self.path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.fields, extrasaction="ignore")
            if new:
                writer.writeheader()
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in self.fields})


class PathLog:
    """Every tracked token's price path, one gzip JSON line per token, for testing
    other entry/exit rules on the same tokens later. Each point is
    [seconds after migration, price USD, liquidity USD, mcap USD, volume 5m USD, buys 5m, sells 5m]."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def append(self, record: dict) -> None:
        with gzip.open(self.path, "at", encoding="utf-8") as fh:
            fh.write(json.dumps(clean(record), separators=(",", ":")) + "\n")


def iter_paths(path: str):
    """Records from a PathLog file, one at a time (the file can hold thousands of tokens).
    A line cut off by a crash ends the stream instead of failing."""
    if not os.path.exists(path):
        return
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except (OSError, EOFError):
        return


def read_paths(path: str) -> list[dict]:
    return list(iter_paths(path))


def read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def clean(obj):
    """Replace NaN/inf with None so the file is strict JSON (browsers reject NaN)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return obj


def write_json_atomic(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(clean(data), fh, ensure_ascii=False, allow_nan=False, default=str)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: the dashboard has the file open for a moment
            if attempt == 4:
                raise
            time.sleep(0.05)


def read_json(path: str):
    """Parsed JSON, or None when missing. A corrupt file is moved aside."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        bad = f"{path}.bad-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            os.replace(path, bad)
        except OSError:
            pass
        logger.error("Could not read %s (%s); moved to %s and starting fresh", path, exc, bad)
        return None
