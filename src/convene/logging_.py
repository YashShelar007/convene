"""One log line per call, in a format you can grep and sum.

Named with a trailing underscore so it never shadows the stdlib ``logging``
module for anything importing from this package.
"""

from __future__ import annotations

import logging

from .config import LOG_DIR, LOG_FILE

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """Module-level logger writing to the convene log file. Idempotent."""
    global _logger
    if _logger is not None:
        return _logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("convene")
    logger.setLevel(logging.INFO)
    # Do not leak into the host application's root logger; a library that
    # prints to someone else's stderr is a bad guest.
    logger.propagate = False

    already = any(
        isinstance(h, logging.FileHandler)
        and getattr(h, "baseFilename", "") == str(LOG_FILE)
        for h in logger.handlers
    )
    if not already:
        handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logger.addHandler(handler)

    _logger = logger
    return logger


def reset_logger() -> None:
    """Drop the cached logger. For tests that redirect ``CONVENE_HOME``."""
    global _logger
    if _logger is not None:
        for handler in list(_logger.handlers):
            handler.close()
            _logger.removeHandler(handler)
    _logger = None
