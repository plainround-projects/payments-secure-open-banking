import pytest

from open_banking.domain.errors import InvalidIdempotencyKeyError, UnbalancedEntryError
from open_banking.domain.idempotency import IdempotencyKey, fingerprint
from open_banking.domain.ledger import Transfer
from open_banking.domain.money import Money
from open_banking.domain.signing import canonical_payment_request

USD = "USD"


def test_a_transfer_is_two_postings_that_sum_to_zero():
    debit, credit = Transfer("acct-ada", "acct-shop", Money(3000, USD)).postings()
    assert (debit.account_id, debit.amount.minor) == ("acct-ada", -3000)
    assert (credit.account_id, credit.amount.minor) == ("acct-shop", 3000)
    assert debit.amount + credit.amount == Money(0, USD)


@pytest.mark.parametrize("minor", [0, -5])
def test_a_transfer_moves_a_positive_amount(minor):
    with pytest.raises(UnbalancedEntryError):
        Transfer("a", "b", Money(minor, USD))


def test_a_transfer_needs_two_different_accounts():
    with pytest.raises(UnbalancedEntryError):
        Transfer("a", "a", Money(1, USD))


def request(**changes):
    values = {
        "client_id": "merchant-1",
        "consent_id": "c-1",
        "payee_account": "acct-shop",
        "amount": Money(3000, USD),
        "idempotency_key": "k-1",
    }
    return canonical_payment_request(**{**values, **changes})


def test_the_signed_bytes_are_stable_and_name_every_field():
    assert request() == request()
    assert request() == (
        b'{"amount_minor":3000,"client_id":"merchant-1","consent_id":"c-1","currency":"USD",'
        b'"idempotency_key":"k-1","payee_account":"acct-shop"}'
    )


@pytest.mark.parametrize(
    "change",
    [
        {"client_id": "merchant-2"},
        {"consent_id": "c-2"},
        {"payee_account": "acct-other-shop"},
        {"amount": Money(3500, USD)},
        {"amount": Money(3000, "EUR")},
        {"idempotency_key": "k-2"},
    ],
)
def test_changing_any_field_changes_what_is_signed(change):
    assert request(**change) != request()


@pytest.mark.parametrize("value", ["", "has space", "a" * 129, "né"])
def test_a_bad_idempotency_key_is_refused(value):
    with pytest.raises(InvalidIdempotencyKeyError):
        IdempotencyKey(value)


def test_the_fingerprint_depends_on_every_field():
    base = fingerprint("initiate-payment", {"a": 1, "b": 2})
    assert base == fingerprint("initiate-payment", {"b": 2, "a": 1})
    assert base != fingerprint("initiate-payment", {"a": 1, "b": 3})
