"""Logging setup: console + rotating file handler."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(log_dir: str = "logs", level: int = logging.INFO) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("polybot")
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger  # already configured (e.g. re-imported)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    # Explicit UTF-8: on Windows the default would be the locale code page,
    # which can't encode every character that shows up in market names.
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "bot.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
