"""Structured logging: one logfmt line per event, with anything shaped like a card number masked."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

# 13 to 19 digits, optionally grouped by spaces or dashes: the shape of a card number (a published test number too).
_CARD_NUMBER = re.compile(r"(?<![\w.])(?:\d[ -]?){13,19}(?![\w])")
_SECRET_FIELDS = {"card_token", "card_number", "cvv", "password", "authorization"}


def redact(text: str) -> str:
    """Mask anything that looks like a card number. Defense in depth: the code never logs card data to begin with."""
    return _CARD_NUMBER.sub("[redacted-number]", text)


def _quote(value: object) -> str:
    text = redact(str(value))
    return json.dumps(text) if not text or any(char in text for char in ' ="\n') else text


class LogfmtFormatter(logging.Formatter):
    """Format a log record as one logfmt line, with the record's extra fields as key=value pairs."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")
        parts = [
            f"ts={timestamp}",
            f"level={record.levelname}",
            f"logger={record.name}",
            f"msg={_quote(record.getMessage())}",
        ]
        extra: dict[str, Any] = getattr(record, "fields", {})
        parts.extend(
            f"{key}={'[withheld]' if key in _SECRET_FIELDS else _quote(value)}" for key, value in extra.items()
        )
        if record.exc_info:
            parts.append(f"exc={_quote(self.formatException(record.exc_info))}")
        return " ".join(parts)


def configure_logging(level: str, stream: TextIO | None = None) -> None:
    """Send every log record to `stream` (stderr by default) as logfmt. Safe to call twice."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(LogfmtFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("uvicorn.access", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(max(logging.getLevelName(level), logging.WARNING))
