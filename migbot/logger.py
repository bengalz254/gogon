"""Logging to the console and to a rotating UTF-8 file (logs/migbot.log)."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from migbot.http import redact


class RedactingFormatter(logging.Formatter):
    """Formats as usual, then removes tokens and API keys (also from tracebacks)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(log_dir: str = "logs", filename: str = "migbot.log", level: int = logging.INFO) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger("migbot")
    root.setLevel(level)
    root.propagate = False
    if root.handlers:
        return root
    fmt = RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    handler = RotatingFileHandler(os.path.join(log_dir, filename), maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(fmt)
    root.addHandler(handler)
    # websocket-client logs through its own logger; keep its errors in our file too.
    ws_logger = logging.getLogger("websocket")
    ws_logger.setLevel(logging.WARNING)
    ws_logger.addHandler(handler)
    return root
