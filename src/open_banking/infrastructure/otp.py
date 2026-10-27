"""The one-time-code channel: where the bank 'sends' the code. The bank's own logs never contain it."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path


class FileOtpChannel:
    """Appends each code as a JSON line to a file that stands in for the customer's phone.

    The file is created readable by its owner only. It is the only place a code is ever written in clear.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def send(self, customer_id: str, code: str, purpose: str) -> None:
        """Append the message."""
        record = {
            "sent_at": datetime.now(UTC).isoformat(),
            "customer_id": customer_id,
            "code": code,
            "purpose": purpose,
        }
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch(mode=0o600, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as outbox:
                outbox.write(json.dumps(record) + "\n")

    def latest_code(self, customer_id: str) -> str | None:
        """The newest code sent to `customer_id`, or None. Reading the phone, for the demo and the customer program."""
        if not self._path.exists():
            return None
        newest: str | None = None
        for line in self._path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["customer_id"] == customer_id:
                newest = str(record["code"])
        return newest

    def latest_any(self) -> str | None:
        """The newest code in the file, whoever it was for: a demo phone has one owner."""
        if not self._path.exists():
            return None
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return str(json.loads(lines[-1])["code"]) if lines else None


class MemoryOtpChannel:
    """Keeps the codes in memory, for tests."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def send(self, customer_id: str, code: str, purpose: str) -> None:
        """Remember the message."""
        self.messages.append((customer_id, code, purpose))

    def latest_code(self, customer_id: str) -> str | None:
        """The newest code sent to `customer_id`, or None."""
        codes = [code for owner, code, _ in self.messages if owner == customer_id]
        return codes[-1] if codes else None
