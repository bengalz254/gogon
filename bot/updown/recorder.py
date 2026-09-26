"""Records every engine input event (with its receive time) to gzipped
JSONL, one file per hour, so a live/paper session can be replayed
bit-for-bit by scripts/updown_backtest.py."""
from __future__ import annotations

import glob
import gzip
import json
import logging
import os
import time
from datetime import datetime, timezone

from bot.updown.events import event_from_dict, event_to_dict


logger = logging.getLogger("polybot.updown.recorder")


class EventRecorder:
    """keep_days: delete recordings older than this many days when a new
    hourly file starts (0 = keep everything). Order-book traffic makes
    recordings large, and a full disk stops the journals too."""

    def __init__(self, directory: str, keep_days: float = 0.0):
        self.directory = directory
        self.keep_days = keep_days
        os.makedirs(directory, exist_ok=True)
        self._fh = None
        self._hour = None
        self._since_flush = 0

    def _file(self, now: float):
        hour = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d-%H")
        if hour != self._hour:
            self.close()
            self._hour = hour
            self._fh = gzip.open(os.path.join(self.directory, f"events-{hour}.jsonl.gz"), "at", encoding="utf-8")
            self.prune(now)
        return self._fh

    def prune(self, now: float) -> int:
        """Delete hourly files older than keep_days (judged by the hour in the name)."""
        if self.keep_days <= 0:
            return 0
        removed = 0
        for path in glob.glob(os.path.join(self.directory, "events-*.jsonl.gz")):
            try:
                stamp = os.path.basename(path)[len("events-"):-len(".jsonl.gz")]
                start = datetime.strptime(stamp, "%Y%m%d-%H").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if now - (start + 3600) > self.keep_days * 86400:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        if removed:
            logger.info("Deleted %d recording file(s) older than %g days", removed, self.keep_days)
        return removed

    def record(self, ev, recv_ts: float | None = None) -> None:
        now = time.time() if recv_ts is None else recv_ts
        fh = self._file(now)
        fh.write(json.dumps({"rt": round(now, 4), "ev": event_to_dict(ev)}, separators=(",", ":")) + "\n")
        self._since_flush += 1
        if self._since_flush >= 500:
            fh.flush()
            self._since_flush = 0

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None


def read_events(paths: list[str]):
    """Yield (recv_ts, event) from recording files, in file order."""
    for path in paths:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    yield float(row["rt"]), event_from_dict(row["ev"])
                except (ValueError, KeyError, TypeError):
                    continue  # tolerate a truncated last line after a crash
