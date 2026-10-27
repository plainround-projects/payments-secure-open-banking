"""Strong customer authentication: two factors (a PIN the customer knows, a one-time code sent to a device)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MAX_ATTEMPTS = 3


class ChallengeStatus(StrEnum):
    """Where an authentication challenge is."""

    PENDING = "PENDING"  # the PIN was right and a code was sent
    VERIFIED = "VERIFIED"  # the code was right; a proof was issued
    LOCKED = "LOCKED"  # too many wrong codes


class AttemptOutcome(StrEnum):
    """What entering a code did."""

    VERIFIED = "verified"
    WRONG_CODE = "wrong_code"
    LOCKED = "locked"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"


@dataclass(frozen=True, slots=True)
class Challenge:
    """A challenge bound to the terms the customer is about to approve; only a digest of the code is stored."""

    challenge_id: str
    customer_id: str
    terms_digest: str
    code_digest: str
    created_at: datetime
    expires_at: datetime
    attempts: int = 0
    status: ChallengeStatus = ChallengeStatus.PENDING

    def attempt(self, code_matches: bool, now: datetime) -> tuple[Challenge, AttemptOutcome]:
        """Enter a code. Returns the challenge as it must now be stored, and what happened.

        A wrong code counts; the third one locks the challenge, and a locked or expired challenge accepts nothing.
        """
        if self.status is ChallengeStatus.VERIFIED:
            return self, AttemptOutcome.ALREADY_USED
        if self.status is ChallengeStatus.LOCKED:
            return self, AttemptOutcome.LOCKED
        if now >= self.expires_at:
            return self, AttemptOutcome.EXPIRED
        if code_matches:
            return dataclasses.replace(self, status=ChallengeStatus.VERIFIED), AttemptOutcome.VERIFIED
        attempts = self.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            return dataclasses.replace(self, attempts=attempts, status=ChallengeStatus.LOCKED), AttemptOutcome.LOCKED
        return dataclasses.replace(self, attempts=attempts), AttemptOutcome.WRONG_CODE
