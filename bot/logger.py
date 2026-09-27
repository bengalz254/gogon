"""Logging setup: console + rotating file handler.

The console is written by a background thread. A Windows console stops
accepting output while text is selected in it (QuickEdit) or its scrollbar
is held, and a direct write would freeze whichever thread logs: in the
Up/Down engine that is the event loop, which then stops reading its
websockets until the console lets go.
"""
from __future__ import annotations

import atexit
import logging
import os
import queue
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler


def setup_logging(log_dir: str = "logs", level: int = logging.INFO, console_stream=None,
                  filename: str = "bot.log") -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("polybot")
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger  # already configured (e.g. re-imported)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(console_stream)
    console.setFormatter(fmt)
    to_console = QueueHandler(queue.SimpleQueue())
    to_console.listener = QueueListener(to_console.queue, console)
    to_console.listener.start()
    atexit.register(_stop_listener, to_console.listener)  # print what is still queued on exit

    # Explicit UTF-8: on Windows the default would be the locale code page,
    # which can't encode every character that shows up in market names.
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, filename), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(to_console)
    logger.addHandler(file_handler)

    # Libraries (websockets, asyncio, ...) log to the root logger. With no
    # handler there, Python writes their warnings straight to the console.
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(to_console)
        root.addHandler(file_handler)
    logging.captureWarnings(True)
    return logger


def _stop_listener(listener: QueueListener) -> None:
    if getattr(listener, "_thread", None) is not None:
        listener.stop()
