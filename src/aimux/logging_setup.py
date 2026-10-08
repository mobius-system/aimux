"""Loguru configuration for aimux's CLI and its long-running bridge processes.

loguru's default sink prints everything from DEBUG up, and the bridge is chatty
by nature: an exec that waits for a command polls ``file.stat`` every 0.3s and
``session.capture`` every 5s, so a single long command produced tens of
thousands of lines and buried anything worth reading (the broker's log on the
deployment host had grown to 185 MB). The floor is INFO now, and the polled
command types are logged at DEBUG (see ``bridge.QUIET_COMMAND_TYPES``), which
means the default output is one line per *meaningful* command.

Set ``AIMUX_LOG_LEVEL=DEBUG`` to get the per-command trace back when you are
diagnosing something.
"""
from __future__ import annotations

import os
import sys

from loguru import logger

DEFAULT_LEVEL = "INFO"
_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")

# loguru's own default format, kept so existing log scrapers keep matching.
_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def setup_logging(level: str | None = None) -> str:
    """Install aimux's single stderr sink; returns the level actually used.

    A level that is not a loguru level name falls back to the default rather
    than raising: a typo in AIMUX_LOG_LEVEL must not take a bridge client down.
    """
    chosen = str(level or os.environ.get("AIMUX_LOG_LEVEL") or DEFAULT_LEVEL).strip().upper()
    if chosen not in _LEVELS:
        chosen = DEFAULT_LEVEL
    logger.remove()
    logger.add(sys.stderr, level=chosen, format=_FORMAT, colorize=None)
    return chosen
