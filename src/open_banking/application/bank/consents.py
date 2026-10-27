"""Consent at the bank: granted only against a proof of strong authentication, revocable by the customer."""

from __future__ import annotations

import logging

from open_banking.application.bank.ports import BankSettings, Crypto, UnitOfWorkFactory
from open_banking.application.logs import fields
from open_banking.application.ports import Clock, IdSource
from open_banking.domain.consent import Consent, ConsentScope, ConsentTerms
from open_banking.domain.errors import (
    AccountNotFoundError,
    AuthenticationFailedError,
    ConsentNotFoundError,
    InvalidProofError,
    UnknownClientError,
)

logger = logging.getLogger(__name__)


class GrantConsent:
    """Create a consent from a proof of authentication. The proof is spent in the same transaction."""

    def __init__(
        self, uow_factory: UnitOfWorkFactory, crypto: Crypto, clock: Clock, ids: IdSource, settings: BankSettings
    ) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._clock = clock
        self._ids = ids
        self._settings = settings

    def execute(self, proof_token: str, terms: ConsentTerms) -> Consent:
        """Raises `InvalidProofError` when the proof is unknown, expired, used, or for other terms."""
        with self._uow_factory() as uow:
            proof = uow.proofs.get_for_update(self._crypto.token_digest(proof_token))
            if proof is None:
                raise InvalidProofError("the proof of authentication is not known")
            if uow.clients.get(terms.client_id) is None:
                raise UnknownClientError(f"no merchant is registered as {terms.client_id!r}")
            if uow.accounts.get(terms.payee_account) is None:
                raise AccountNotFoundError(f"no account {terms.payee_account!r}")
            time_to_live = (
                self._settings.consent_time_to_live
                if terms.scope is ConsentScope.SINGLE_PAYMENT
                else self._settings.recurring_consent_time_to_live
            )
            consent = Consent.grant(str(self._ids.new_id()), proof, terms, self._clock.now(), time_to_live)
            uow.proofs.mark_consumed(proof.proof_id)
            uow.consents.add(consent)
            logger.info(
                "consent granted",
                extra=fields(consent_id=consent.consent_id, scope=terms.scope.value, client_id=terms.client_id),
            )
            return consent


class RevokeConsent:
    """The customer withdraws a consent, independently of the merchant. Revoking again changes nothing."""

    def __init__(self, uow_factory: UnitOfWorkFactory, crypto: Crypto, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._clock = clock
        self._decoy = crypto.hash_secret("not-a-customer-pin")

    def execute(self, username: str, pin: str, consent_id: str) -> Consent:
        """Needs the customer's PIN (withdrawing authority needs one factor, granting it needs two)."""
        with self._uow_factory() as uow:
            customer = uow.customers.by_username(username)
            pin_ok = self._crypto.verify_secret(pin, customer.pin_hash if customer else self._decoy)
            if customer is None or not pin_ok:
                raise AuthenticationFailedError("the username or PIN is not right")
            consent = uow.consents.get_for_update(consent_id)
            if consent is None or consent.customer_id != customer.customer_id:
                raise ConsentNotFoundError(f"no consent {consent_id!r}")
            revoked = consent.revoked(self._clock.now())
            if revoked is not consent:
                uow.consents.save(revoked)
                logger.info("consent revoked", extra=fields(consent_id=consent_id))
            return revoked
