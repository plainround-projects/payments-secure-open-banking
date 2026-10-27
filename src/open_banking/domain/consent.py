"""Consent: a scoped, expiring, revocable grant created only after strong customer authentication."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from open_banking.domain.errors import InvalidProofError
from open_banking.domain.money import Money


class ConsentScope(StrEnum):
    """`single-payment` allows one payment up to the maximum; `recurring` allows payments up to a cap per period."""

    SINGLE_PAYMENT = "single-payment"
    RECURRING = "recurring"


class Refusal(StrEnum):
    """Why a payment was refused. One code per reason, never a generic "denied"."""

    CONSENT_NOT_FOUND = "consent_not_found"
    NOT_GRANTED_TO_CALLER = "consent_not_granted_to_caller"
    REQUEST_NOT_AS_SIGNED = "request_not_as_signed"
    PAYEE_NOT_COVERED = "payee_not_covered"
    CONSENT_REVOKED = "consent_revoked"
    CONSENT_EXPIRED = "consent_expired"
    CONSENT_ALREADY_USED = "consent_already_used"
    CURRENCY_NOT_COVERED = "currency_not_covered"
    AMOUNT_EXCEEDS_CONSENT = "amount_exceeds_consent"
    PERIOD_CAP_EXCEEDED = "period_cap_exceeded"
    INSUFFICIENT_FUNDS = "insufficient_funds"


@dataclass(frozen=True, slots=True)
class ConsentTerms:
    """What the customer approves at the bank: who may pay whom, up to how much, how often.

    `period` applies to recurring consents: `max_amount` is the cap for any window of that length.
    """

    payer_account: str
    payee_account: str
    client_id: str
    max_amount: Money
    scope: ConsentScope
    period: timedelta | None = None

    def __post_init__(self) -> None:
        if not self.max_amount.is_positive():
            raise InvalidProofError("a consent needs a positive maximum amount")
        if (self.scope is ConsentScope.RECURRING) != (self.period is not None):
            raise InvalidProofError("a recurring consent needs a period and a single-payment consent has none")

    def digest(self) -> str:
        """A SHA-256 of the terms. A proof of authentication carries it, so the customer's approval is tied to
        exactly these terms (dynamic linking): a proof for 50.00 cannot create a consent for 5,000.00."""
        document = {
            "client_id": self.client_id,
            "currency": self.max_amount.currency,
            "max_amount_minor": self.max_amount.minor,
            "payee_account": self.payee_account,
            "payer_account": self.payer_account,
            "period_seconds": None if self.period is None else int(self.period.total_seconds()),
            "scope": self.scope.value,
        }
        return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthenticationProof:
    """Evidence that a customer passed strong authentication for exactly these terms, once, recently."""

    proof_id: str
    customer_id: str
    terms_digest: str
    expires_at: datetime
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class PaymentRequest:
    """What the merchant asks for under a consent."""

    consent_id: str
    payee_account: str
    amount: Money


@dataclass(frozen=True, slots=True)
class Consent:
    """A consent as stored. Immutable; revocation and use return new values."""

    consent_id: str
    customer_id: str
    terms: ConsentTerms
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    used_at: datetime | None = None

    @classmethod
    def grant(
        cls,
        consent_id: str,
        proof: AuthenticationProof,
        terms: ConsentTerms,
        now: datetime,
        time_to_live: timedelta,
    ) -> Consent:
        """Create a consent. Impossible without a valid proof that was issued for exactly these terms."""
        if proof.consumed:
            raise InvalidProofError("the proof of authentication was already used")
        if now >= proof.expires_at:
            raise InvalidProofError("the proof of authentication has expired")
        if proof.terms_digest != terms.digest():
            raise InvalidProofError("the proof of authentication was issued for different terms")
        return cls(consent_id, proof.customer_id, terms, now, now + time_to_live)

    def revoked(self, now: datetime) -> Consent:
        """The consent after the customer withdrew it. Revoking twice keeps the first time."""
        return self if self.revoked_at is not None else dataclasses.replace(self, revoked_at=now)

    def marked_used(self, now: datetime) -> Consent:
        """The consent after a payment was made under it."""
        return dataclasses.replace(self, used_at=now)

    def refusal_for(
        self, request: PaymentRequest, caller_client_id: str, now: datetime, spent_in_period: Money
    ) -> Refusal | None:
        """The first reason this payment may not happen under this consent, or None when it may.

        The order is fixed: whose consent, which payee, revoked, expired, already used, currency, amount, period cap.
        """
        terms = self.terms
        if self.terms.client_id != caller_client_id:
            return Refusal.NOT_GRANTED_TO_CALLER
        if request.payee_account != terms.payee_account:
            return Refusal.PAYEE_NOT_COVERED
        if self.revoked_at is not None:
            return Refusal.CONSENT_REVOKED
        if now >= self.expires_at:
            return Refusal.CONSENT_EXPIRED
        if terms.scope is ConsentScope.SINGLE_PAYMENT and self.used_at is not None:
            return Refusal.CONSENT_ALREADY_USED
        if request.amount.currency != terms.max_amount.currency:
            return Refusal.CURRENCY_NOT_COVERED
        if request.amount > terms.max_amount:
            return Refusal.AMOUNT_EXCEEDS_CONSENT
        if terms.scope is ConsentScope.RECURRING and spent_in_period + request.amount > terms.max_amount:
            return Refusal.PERIOD_CAP_EXCEEDED
        return None

    def explain(self, refusal: Refusal, request: PaymentRequest, spent_in_period: Money) -> str:
        """A clear sentence for a refusal, with the numbers that matter."""
        terms = self.terms
        messages: dict[Refusal, Callable[[], str]] = {
            Refusal.CONSENT_NOT_FOUND: lambda: "consent not found",
            Refusal.NOT_GRANTED_TO_CALLER: lambda: "consent was not granted to this merchant",
            Refusal.REQUEST_NOT_AS_SIGNED: lambda: (
                "amount does not match approved consent: the request signature does not cover this request"
            ),
            Refusal.PAYEE_NOT_COVERED: lambda: "consent does not cover this payee",
            Refusal.CONSENT_REVOKED: lambda: "consent revoked",
            Refusal.CONSENT_EXPIRED: lambda: "consent expired",
            Refusal.CONSENT_ALREADY_USED: lambda: "single-payment consent already used",
            Refusal.CURRENCY_NOT_COVERED: lambda: f"consent is in {terms.max_amount.currency}",
            Refusal.AMOUNT_EXCEEDS_CONSENT: lambda: (
                f"amount {request.amount.format()} exceeds consent max {terms.max_amount.format()}"
            ),
            Refusal.PERIOD_CAP_EXCEEDED: lambda: (
                f"amount {request.amount.format()} would take this period's total to "
                f"{(spent_in_period + request.amount).format()}, over the consent cap {terms.max_amount.format()}"
            ),
            Refusal.INSUFFICIENT_FUNDS: lambda: "the payer has insufficient funds",
        }
        return messages[refusal]()
