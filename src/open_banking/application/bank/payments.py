"""Payment initiation: money moves only under a valid consent, exactly once, for the amount that was signed."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from open_banking.application.bank.idempotent import IdempotentExecutor
from open_banking.application.bank.ports import Crypto, PaymentRecord, UnitOfWork, UnitOfWorkFactory
from open_banking.application.logs import fields
from open_banking.application.ports import Clock, IdSource
from open_banking.domain.consent import ConsentScope, PaymentRequest, Refusal
from open_banking.domain.errors import (
    AccountNotFoundError,
    ClientAuthenticationError,
    PaymentNotFoundError,
    PaymentRefusedError,
)
from open_banking.domain.idempotency import IdempotencyKey, StoredResponse, fingerprint
from open_banking.domain.ledger import Transfer
from open_banking.domain.money import Money
from open_banking.domain.signing import canonical_payment_request

logger = logging.getLogger(__name__)


def payment_body(payment: PaymentRecord) -> dict[str, Any]:
    """A payment as the API shows it."""
    return {
        "payment_id": payment.payment_id,
        "status": "POSTED",
        "consent_id": payment.consent_id,
        "payer_account": payment.payer_account,
        "payee_account": payment.payee_account,
        "amount_minor": payment.amount.minor,
        "currency": payment.amount.currency,
        "amount": payment.amount.format(),
        "created_at": payment.created_at.isoformat(),
    }


@dataclass(frozen=True, slots=True)
class PaymentResult:
    """The response to send, and whether it was replayed from the key's stored answer."""

    response: StoredResponse
    replayed: bool


class InitiatePayment:
    """Make the payment a merchant asks for, if and only if a consent covers it.

    In one transaction: take the (merchant, key) lock; check the request signature; lock the consent and check every
    rule on it; lock the payer's account; post the balanced transfer; store the answer under the key. A replay returns
    the stored answer and checks nothing again. Any refusal rolls everything back and stores nothing.
    """

    def __init__(
        self,
        executor: IdempotentExecutor,
        uow_factory: UnitOfWorkFactory,
        crypto: Crypto,
        clock: Clock,
        ids: IdSource,
    ) -> None:
        self._executor = executor
        self._crypto = crypto
        self._clock = clock
        self._ids = ids
        del uow_factory  # the executor owns the transaction

    def execute(
        self, client_id: str, key: IdempotencyKey, request: PaymentRequest, signature: str | None
    ) -> PaymentResult:
        """Raises `PaymentRefusedError` with one distinct reason for every refusal."""
        request_fingerprint = fingerprint(
            "initiate-payment",
            {
                "consent_id": request.consent_id,
                "payee_account": request.payee_account,
                "amount_minor": request.amount.minor,
                "currency": request.amount.currency,
            },
        )

        def work(uow: UnitOfWork) -> StoredResponse:
            return self._pay(uow, client_id, key, request, signature)

        execution = self._executor.execute(
            client_id=client_id, key=key, request_fingerprint=request_fingerprint, work=work
        )
        return PaymentResult(execution.response, execution.replayed)

    def _pay(
        self, uow: UnitOfWork, client_id: str, key: IdempotencyKey, request: PaymentRequest, signature: str | None
    ) -> StoredResponse:
        now = self._clock.now()
        client = uow.clients.get(client_id)
        if client is None:
            raise ClientAuthenticationError("invalid merchant credential")
        signed = canonical_payment_request(
            client_id=client_id,
            consent_id=request.consent_id,
            payee_account=request.payee_account,
            amount=request.amount,
            idempotency_key=key.value,
        )
        if not signature or not self._crypto.verify_signature(client.public_jwk, signed, signature):
            raise self._refused(
                Refusal.REQUEST_NOT_AS_SIGNED,
                "amount does not match approved consent: the request signature does not cover this request",
                client_id,
                request,
            )
        consent = uow.consents.get_for_update(request.consent_id)
        if consent is None:
            raise self._refused(Refusal.CONSENT_NOT_FOUND, "consent not found", client_id, request)
        spent = self._spent_in_period(
            uow, consent.consent_id, consent.terms.scope, consent.terms.period, now, request.amount
        )
        refusal = consent.refusal_for(request, client_id, now, spent)
        if refusal is not None:
            raise self._refused(refusal, consent.explain(refusal, request, spent), client_id, request)
        # Lock both accounts in one fixed order, so two payments in opposite directions cannot wait for each other.
        locked = {
            account_id: uow.accounts.get_for_update(account_id)
            for account_id in sorted({consent.terms.payer_account, request.payee_account})
        }
        payer = locked[consent.terms.payer_account]
        payee = locked[request.payee_account]
        if payer is None or payee is None:
            raise AccountNotFoundError("the payer or the payee account no longer exists")
        if payer.balance < request.amount:
            raise self._refused(
                Refusal.INSUFFICIENT_FUNDS,
                consent.explain(Refusal.INSUFFICIENT_FUNDS, request, spent),
                client_id,
                request,
            )
        payment = PaymentRecord(
            payment_id=str(self._ids.new_id()),
            consent_id=consent.consent_id,
            client_id=client_id,
            payer_account=payer.account_id,
            payee_account=payee.account_id,
            amount=request.amount,
            created_at=now,
        )
        uow.ledger.post(
            Transfer(payer.account_id, payee.account_id, request.amount), "PAYMENT", payment.payment_id, now
        )
        uow.payments.add(payment)
        if consent.terms.scope is ConsentScope.SINGLE_PAYMENT:
            uow.consents.save(consent.marked_used(now))
        logger.info(
            "payment posted",
            extra=fields(
                payment_id=payment.payment_id,
                consent_id=consent.consent_id,
                client_id=client_id,
                amount=request.amount.format(),
            ),
        )
        return StoredResponse(201, payment_body(payment))

    @staticmethod
    def _spent_in_period(
        uow: UnitOfWork, consent_id: str, scope: ConsentScope, period: Any, now: datetime, amount: Money
    ) -> Money:
        if scope is not ConsentScope.RECURRING or period is None:
            return Money.zero(amount.currency)
        return uow.consents.spent_since(consent_id, now - period, amount.currency)

    @staticmethod
    def _refused(refusal: Refusal, message: str, client_id: str, request: PaymentRequest) -> PaymentRefusedError:
        logger.warning(
            "payment refused",
            extra=fields(refusal=refusal.value, client_id=client_id, consent_id=request.consent_id),
        )
        return PaymentRefusedError(refusal.value, message)


class GetPayment:
    """A merchant reads a payment it initiated."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def execute(self, client_id: str, payment_id: str) -> dict[str, Any]:
        """Raises `PaymentNotFoundError` for an unknown payment or one made for another merchant."""
        with self._uow_factory() as uow:
            payment = uow.payments.get(payment_id, client_id)
            if payment is None:
                raise PaymentNotFoundError(f"no payment {payment_id!r}")
            return payment_body(payment)
