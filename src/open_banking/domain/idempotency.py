"""The idempotency key, the fingerprint of a request, and the response stored under a key."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from open_banking.domain.errors import InvalidIdempotencyKeyError

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    """A client-chosen key: 1 to 128 characters from letters, digits and `_ . : -`."""

    value: str

    def __post_init__(self) -> None:
        if not _KEY.match(self.value):
            raise InvalidIdempotencyKeyError(
                "the Idempotency-Key must be 1 to 128 characters of letters, digits and _ . : -"
            )


def fingerprint(operation: str, fields: Mapping[str, Any]) -> str:
    """A SHA-256 over the operation and its fields in a canonical form: the same request always gives the same text."""
    canonical = json.dumps({"operation": operation, "fields": fields}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredResponse:
    """The HTTP status and JSON body a key was answered with; a repeat gets exactly this."""

    status: int
    body: Mapping[str, Any]
