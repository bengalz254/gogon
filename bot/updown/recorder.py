"""Records every engine input event (with its receive time) to gzipped
JSONL, one file per hour, so a live/paper session can be replayed
bit-for-bit by scripts/updown_backtest.py."""
from __future__ import annotations

import gzip
import json
import os
import time
from datetime import datetime, timezone

from bot.updown.events import event_from_dict, event_to_dict


class EventRecorder:
    def __init__(self, directory: str):
        self.directory = directory
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
        return self._fh

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
