"""Small flows shared by the tests: authenticate and grant a consent, sign and make a payment."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from open_banking.domain.consent import Consent, ConsentScope, ConsentTerms, PaymentRequest
from open_banking.domain.idempotency import IdempotencyKey, StoredResponse
from open_banking.domain.money import Money
from open_banking.domain.signing import canonical_payment_request
from open_banking.infrastructure import keys
from tests.unit.conftest import BANK_URL, Merchant

USD = "USD"


def terms(
    payer: str = "acct-ada",
    payee: str = "acct-shop",
    client: str = "merchant-1",
    maximum: int = 5000,
    scope: ConsentScope = ConsentScope.SINGLE_PAYMENT,
    period: timedelta | None = None,
) -> ConsentTerms:
    return ConsentTerms(payer, payee, client, Money(maximum, USD), scope, period)


def authenticate(world: Any, username: str, pin: str, consent_terms: ConsentTerms) -> str:
    """PIN, then the code from the phone. Returns the proof's bearer value."""
    started = world.bank.start_challenge.execute(username, pin, consent_terms)
    customer = world.db.customers_by_name(username)
    code = world.phone.latest_code(customer.customer_id)
    assert code is not None
    verification = world.bank.verify_challenge.execute(started.challenge_id, code)
    assert verification.proof is not None
    return str(verification.proof)


def grant(world: Any, consent_terms: ConsentTerms | None = None, username: str = "ada") -> Consent:
    """A consent granted the proper way."""
    consent_terms = consent_terms or terms()
    pin = world.ada_pin if username == "ada" else world.bob_pin
    granted: Consent = world.bank.grant_consent.execute(
        authenticate(world, username, pin, consent_terms), consent_terms
    )
    return granted


def sign_request(
    merchant: Merchant, consent_id: str, payee: str, amount: Money, key: str, client_id: str | None = None
) -> str:
    return keys.sign(
        merchant.private_pem,
        canonical_payment_request(
            client_id=client_id or merchant.client_id,
            consent_id=consent_id,
            payee_account=payee,
            amount=amount,
            idempotency_key=key,
        ),
    )


def pay(
    world: Any,
    consent_id: str,
    amount_minor: int,
    key: str,
    *,
    merchant: Merchant | None = None,
    payee: str = "acct-shop",
    signed_minor: int | None = None,
) -> tuple[StoredResponse, bool]:
    """A payment request signed by `merchant` (merchant-1 by default), straight to the use case."""
    merchant = merchant or world.merchant
    signature = sign_request(merchant, consent_id, payee, Money(signed_minor or amount_minor, USD), key)
    result = world.bank.initiate_payment.execute(
        merchant.client_id,
        IdempotencyKey(key),
        PaymentRequest(consent_id, payee, Money(amount_minor, USD)),
        signature,
    )
    return result.response, result.replayed


def dpop(merchant: Merchant, world: Any, method: str, path: str, token: str | None = None) -> str:
    """A proof of possession from `merchant`'s key for one request."""
    return keys.build_dpop_proof(
        merchant.private_pem,
        merchant.public_jwk,
        method=method,
        url=f"{BANK_URL}{path}",
        now=world.clock.now(),
        access_token=token,
    )
