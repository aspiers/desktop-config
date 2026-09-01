"""Service logging with sd-daemon priorities for journald.

The service units capture stderr into journald, which already attributes
lines via SyslogIdentifier and parses ``<N>`` sd-daemon prefixes into real
PRIORITY fields — so ``journalctl -p warning`` can surface failures without
the routine narration. Plain prints carried neither structure.
"""

from __future__ import annotations

import logging
import sys
from typing import Final, TextIO

_SD_PRIORITIES: Final = {
    logging.DEBUG: 7,
    logging.INFO: 6,
    logging.WARNING: 4,
    logging.ERROR: 3,
    logging.CRITICAL: 2,
}


class _SdDaemonFormatter(logging.Formatter):
    """Prefix each line with the sd-daemon priority journald understands."""

    def format(self, record: logging.LogRecord) -> str:
        priority = _SD_PRIORITIES.get(record.levelno, 6)
        return f"<{priority}>{record.getMessage()}"


class _CurrentStderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Write to whatever sys.stderr is at emit time.

    StreamHandler captures the stream once at construction, which breaks
    pytest's capsys redirection and any later stderr replacement.
    """

    @property
    def stream(self) -> TextIO:
        """Return the live stderr."""
        return sys.stderr

    @stream.setter
    def stream(self, value: object) -> None:
        """Ignore assignment; the live stderr is always used."""


def service_logger(name: str) -> logging.Logger:
    """Return a configured logger writing sd-daemon-prefixed lines to stderr."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = _CurrentStderrHandler()
        handler.setFormatter(_SdDaemonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
