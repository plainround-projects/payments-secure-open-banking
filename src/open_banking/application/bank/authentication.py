"""Strong customer authentication at the bank: a PIN, then a one-time code, between the customer and the bank only."""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from datetime import datetime

from open_banking.application.bank.ports import BankSettings, Crypto, OtpChannel, UnitOfWorkFactory
from open_banking.application.logs import fields
from open_banking.application.ports import Clock, IdSource
from open_banking.domain.authentication import MAX_ATTEMPTS, AttemptOutcome, Challenge
from open_banking.domain.consent import AuthenticationProof, ConsentTerms
from open_banking.domain.errors import (
    AccountNotFoundError,
    AccountNotOwnedError,
    AuthenticationFailedError,
    ChallengeNotFoundError,
    UnknownClientError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChallengeStarted:
    """A challenge was created and a code sent to the customer's device."""

    challenge_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Verification:
    """The result of entering a code. `proof` is the bearer value of the proof, shown once, when the code was right."""

    outcome: AttemptOutcome
    proof: str | None
    proof_expires_at: datetime | None
    attempts_left: int


class StartChallenge:
    """Check the customer's PIN and send a code, for exactly the terms the customer is about to approve."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        crypto: Crypto,
        otp: OtpChannel,
        clock: Clock,
        ids: IdSource,
        settings: BankSettings,
    ) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._otp = otp
        self._clock = clock
        self._ids = ids
        self._settings = settings
        self._decoy = crypto.hash_secret("not-a-customer-pin")  # so an unknown user costs the same as a wrong PIN

    def execute(self, username: str, pin: str, terms: ConsentTerms) -> ChallengeStarted:
        """Raises `AuthenticationFailedError` for an unknown user or a wrong PIN (one answer for both)."""
        with self._uow_factory() as uow:
            customer = uow.customers.by_username(username)
            pin_ok = self._crypto.verify_secret(pin, customer.pin_hash if customer else self._decoy)
            if customer is None or not pin_ok:
                logger.warning("authentication failed", extra=fields(username=username))
                raise AuthenticationFailedError("the username or PIN is not right")
            if terms.payer_account != customer.account_id:
                raise AccountNotOwnedError("the payer account does not belong to this customer")
            if uow.clients.get(terms.client_id) is None:
                raise UnknownClientError(f"no merchant is registered as {terms.client_id!r}")
            if uow.accounts.get(terms.payee_account) is None:
                raise AccountNotFoundError(f"no account {terms.payee_account!r}")
            now = self._clock.now()
            challenge_id = str(self._ids.new_id())
            code = self._crypto.new_code()
            challenge = Challenge(
                challenge_id=challenge_id,
                customer_id=customer.customer_id,
                terms_digest=terms.digest(),
                code_digest=self._crypto.code_digest(challenge_id, code),
                created_at=now,
                expires_at=now + self._settings.challenge_time_to_live,
            )
            uow.challenges.add(challenge)
            self._otp.send(customer.customer_id, code, "approve a payment consent")
            logger.info("challenge started", extra=fields(challenge_id=challenge_id, customer_id=customer.customer_id))
            return ChallengeStarted(challenge_id, challenge.expires_at)


class VerifyChallenge:
    """Check the one-time code. A right code issues a proof for the challenge's terms; a wrong one counts."""

    def __init__(self, uow_factory: UnitOfWorkFactory, crypto: Crypto, clock: Clock, settings: BankSettings) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._clock = clock
        self._settings = settings

    def execute(self, challenge_id: str, code: str) -> Verification:
        """Wrong codes, locks and expiry are results, not errors, so the attempt is stored."""
        with self._uow_factory() as uow:
            challenge = uow.challenges.get_for_update(challenge_id)
            if challenge is None:
                raise ChallengeNotFoundError(f"no challenge {challenge_id!r}")
            now = self._clock.now()
            matches = hmac.compare_digest(self._crypto.code_digest(challenge_id, code), challenge.code_digest)
            updated, outcome = challenge.attempt(matches, now)
            uow.challenges.save(updated)
            if outcome is not AttemptOutcome.VERIFIED:
                logger.warning("code not accepted", extra=fields(challenge_id=challenge_id, outcome=outcome.value))
                return Verification(outcome, None, None, max(MAX_ATTEMPTS - updated.attempts, 0))
            token = self._crypto.new_token()
            expires_at = now + self._settings.proof_time_to_live
            uow.proofs.add(
                AuthenticationProof(
                    self._crypto.token_digest(token), challenge.customer_id, challenge.terms_digest, expires_at
                )
            )
            logger.info("strong authentication succeeded", extra=fields(challenge_id=challenge_id))
            return Verification(outcome, token, expires_at, MAX_ATTEMPTS)
