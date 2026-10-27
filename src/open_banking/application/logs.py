"""Structured-log helper shared by the use cases: `logger.info("msg", extra=fields(payment_id=...))`."""

from __future__ import annotations

from typing import Any


def fields(**values: Any) -> dict[str, Any]:
    """Attach key=value pairs to a log record. Never pass card data, tokens or secrets."""
    return {"fields": values}
