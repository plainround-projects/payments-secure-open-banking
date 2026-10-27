"""The real clock and the real id source."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID


class SystemClock:
    """The current UTC time."""

    def now(self) -> datetime:
        """Now, timezone-aware."""
        return datetime.now(UTC)


class RandomIds:
    """Random version-4 UUIDs."""

    def new_id(self) -> UUID:
        """A fresh UUID."""
        return uuid.uuid4()


class OffsetClock:
    """The real clock plus an offset that only moves forward, so a demo can let a consent expire without waiting."""

    def __init__(self, base: SystemClock | None = None) -> None:
        self._base = base or SystemClock()
        self._offset = timedelta(0)

    def advance(self, delta: timedelta) -> None:
        """Move this clock ahead."""
        self._offset += delta

    def now(self) -> datetime:
        """Real time plus the offset."""
        return self._base.now() + self._offset
