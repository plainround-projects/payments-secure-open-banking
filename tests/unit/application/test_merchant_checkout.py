"""The merchant's side: a checkout is completed with the customer's consent id, and a retry is always safe."""

import pytest

from open_banking.application.merchant.checkout import (
    BankUnavailableError,
    Checkout,
    CheckoutNotFoundError,
    CheckoutStatus,
    CompleteCheckout,
    CreateCheckout,
    InMemoryCheckouts,
    MerchantProfile,
    Paid,
    Refused,
)
from open_banking.domain.money import Money
from tests.unit.fakes import SequentialIds


class ScriptedGateway:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def initiate_payment(self, *, consent_id, payee_account, amount, idempotency_key):
        self.calls.append((consent_id, payee_account, amount, idempotency_key))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def parts(*answers):
    checkouts = InMemoryCheckouts()
    gateway = ScriptedGateway(*answers)
    create = CreateCheckout(checkouts, SequentialIds())
    complete = CompleteCheckout(checkouts, gateway, MerchantProfile("merchant-1", "acct-shop"))
    return create, complete, gateway, checkouts


def test_a_checkout_starts_awaiting_the_customers_consent():
    create, _, _, checkouts = parts()
    checkout = create.execute(Money(3000, "USD"))
    assert checkout.status is CheckoutStatus.AWAITING_CONSENT and checkouts.get(checkout.checkout_id) == checkout


def test_completing_with_a_consent_pays_and_asks_the_bank_for_the_merchants_own_payee():
    create, complete, gateway, _ = parts(Paid("p-1"))
    checkout = create.execute(Money(3000, "USD"))
    paid = complete.execute(checkout.checkout_id, "c-1")
    assert (paid.status, paid.payment_id, paid.consent_id) == (CheckoutStatus.PAID, "p-1", "c-1")
    assert gateway.calls == [("c-1", "acct-shop", Money(3000, "USD"), f"checkout-{checkout.checkout_id}-c-1")]


def test_completing_a_paid_checkout_again_pays_nothing_more():
    create, complete, gateway, _ = parts(Paid("p-1"))
    checkout = create.execute(Money(3000, "USD"))
    first = complete.execute(checkout.checkout_id, "c-1")
    assert complete.execute(checkout.checkout_id, "c-1") == first and len(gateway.calls) == 1


def test_a_refusal_keeps_the_checkout_open_with_the_banks_reason_and_another_consent_gets_another_key():
    create, complete, gateway, _ = parts(Refused("consent_expired", "consent expired"), Paid("p-2"))
    checkout = create.execute(Money(3000, "USD"))
    refused = complete.execute(checkout.checkout_id, "c-1")
    assert refused.status is CheckoutStatus.AWAITING_CONSENT and refused.refusal == Refused(
        "consent_expired", "consent expired"
    )
    paid = complete.execute(checkout.checkout_id, "c-2")
    assert paid.status is CheckoutStatus.PAID and paid.refusal is None
    assert gateway.calls[0][3] != gateway.calls[1][3]


def test_when_the_bank_cannot_be_reached_the_outcome_is_unknown_and_the_retry_uses_the_same_key():
    create, complete, gateway, checkouts = parts(BankUnavailableError("no answer"), Paid("p-1"))
    checkout = create.execute(Money(3000, "USD"))
    with pytest.raises(BankUnavailableError):
        complete.execute(checkout.checkout_id, "c-1")
    stored = checkouts.get(checkout.checkout_id)
    assert isinstance(stored, Checkout) and stored.status is CheckoutStatus.UNKNOWN
    assert complete.execute(checkout.checkout_id, "c-1").status is CheckoutStatus.PAID
    assert gateway.calls[0][3] == gateway.calls[1][3]


def test_an_unknown_checkout_is_an_error():
    _, complete, _, _ = parts()
    with pytest.raises(CheckoutNotFoundError):
        complete.execute("nope", "c-1")
