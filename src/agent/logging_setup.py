"""Logging that cannot leak secrets.

Every formatted log line (message, arguments and tracebacks) passes through `redact`
before it is written, so a key stays out of the log even if a library puts a full URL
into an exception message.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

MASK = "***"
# Any "api-key=..." / "api_key=..." / "apikey=..." value, even for a key we do not know.
_KEY_PARAM = re.compile(r"(?i)(api[-_]?key=)[^&\s\"'<>]+")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    return _KEY_PARAM.sub(rf"\1{MASK}", text)


# Log times on a 12-hour clock in local time, e.g. "2026-11-12 09:05:27 PM".
DATE_FORMAT = "%Y-%m-%d %I:%M:%S %p"


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt, datefmt=DATE_FORMAT)
        self._secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


def setup_logging(log_file: Path | None, *, console: bool, secrets: Iterable[str] = ()) -> None:
    """Send logs to a rotating file (5 x 10 MB) and, optionally, the console."""
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", secrets)
    handlers: list[logging.Handler] = []
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        )
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    # The websocket library logs connection details at DEBUG; keep it at WARNING.
    logging.getLogger("websockets").setLevel(logging.WARNING)
