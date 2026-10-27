"""The ports both sides share: the clock and the source of identifiers, injected so tests are deterministic."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID


class Clock(Protocol):
    """The current time, injected so tests are deterministic."""

    def now(self) -> datetime:
        """A timezone-aware UTC time."""
        ...


class IdSource(Protocol):
    """New identifiers, injected for the same reason."""

    def new_id(self) -> UUID:
        """A fresh UUID."""
        ...
