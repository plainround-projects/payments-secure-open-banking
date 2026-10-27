"""The lesson's steps 4 to 6: money moves only under a valid consent, once, for the signed amount."""

import threading
from datetime import timedelta
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from open_banking.domain.consent import ConsentScope, PaymentRequest
from open_banking.domain.errors import (
    ClientAuthenticationError,
    IdempotencyKeyReusedError,
    PaymentNotFoundError,
    PaymentRefusedError,
)
from open_banking.domain.idempotency import IdempotencyKey
from open_banking.domain.money import Money
from tests.unit.helpers import grant, pay, sign_request, terms


def refused(world, consent_id, amount, key, **kwargs) -> PaymentRefusedError:
    with pytest.raises(PaymentRefusedError) as caught:
        pay(world, consent_id, amount, key, **kwargs)
    return caught.value


def test_a_payment_within_the_consent_moves_exactly_that_money_and_balances_the_ledger(world):
    consent = grant(world)
    response, replayed = pay(world, consent.consent_id, 3000, "k-1")
    assert response.status == 201 and not replayed
    assert response.body["status"] == "POSTED" and response.body["amount_minor"] == 3000
    assert (world.db.balance("acct-ada"), world.db.balance("acct-shop")) == (47_000, 3000)
    assert world.db.ledger_sum() == 0
    assert world.db.consents[consent.consent_id].used_at is not None


def test_over_the_amount_another_payee_and_after_expiry_are_each_refused_with_a_clear_reason(world):
    consent = grant(world)
    over = refused(world, consent.consent_id, 6000, "k-over")
    assert (over.refusal, over.message) == ("amount_exceeds_consent", "amount 60.00 exceeds consent max 50.00")
    other = refused(world, consent.consent_id, 3000, "k-payee", payee="acct-other-shop")
    assert (other.refusal, other.message) == ("payee_not_covered", "consent does not cover this payee")
    world.clock.advance(timedelta(minutes=10, seconds=1))
    late = refused(world, consent.consent_id, 3000, "k-late")
    assert (late.refusal, late.message) == ("consent_expired", "consent expired")
    assert world.db.balance("acct-ada") == 50_000 and world.db.ledger_sum() == 0


def test_a_refusal_changes_nothing_and_stores_nothing(world):
    consent = grant(world)
    refused(world, consent.consent_id, 6000, "k-over")
    assert world.db.payments == {} and world.db.keys == {}
    response, _ = pay(world, consent.consent_id, 3000, "k-over")  # the same key, now a good request, works
    assert response.status == 201


def test_a_replay_with_the_same_key_returns_the_stored_answer_and_moves_no_money(world):
    consent = grant(world)
    first, first_replayed = pay(world, consent.consent_id, 3000, "k-1")
    second, second_replayed = pay(world, consent.consent_id, 3000, "k-1")
    assert (first_replayed, second_replayed) == (False, True)
    assert first == second and len(world.db.payments) == 1
    assert world.db.balance("acct-ada") == 47_000


def test_a_replay_still_works_after_the_consent_was_revoked_because_the_money_already_moved(world):
    consent = grant(world)
    first, _ = pay(world, consent.consent_id, 3000, "k-1")
    world.bank.revoke_consent.execute("ada", world.ada_pin, consent.consent_id)
    second, replayed = pay(world, consent.consent_id, 3000, "k-1")
    assert replayed and second == first and len(world.db.payments) == 1


def test_a_key_reused_for_a_different_request_is_refused_not_replayed(world):
    consent = grant(world, terms(maximum=10_000, scope=ConsentScope.RECURRING, period=timedelta(days=30)))
    pay(world, consent.consent_id, 3000, "k-1")
    with pytest.raises(IdempotencyKeyReusedError):
        pay(world, consent.consent_id, 3100, "k-1")
    assert len(world.db.payments) == 1


def test_two_merchants_may_use_the_same_key(world):
    own = grant(world)
    other_terms = terms(client="merchant-2", payee="acct-other-shop")
    theirs = grant(world, other_terms)
    first, _ = pay(world, own.consent_id, 3000, "same-key")
    second, replayed = pay(
        world, theirs.consent_id, 3000, "same-key", merchant=world.other_merchant, payee="acct-other-shop"
    )
    assert not replayed and first.body["payment_id"] != second.body["payment_id"]


def test_a_changed_amount_is_detected_because_the_signature_no_longer_matches(world):
    consent = grant(world)
    error = refused(world, consent.consent_id, 3500, "k-1", signed_minor=3000)
    assert error.refusal == "request_not_as_signed"
    assert error.message == "amount does not match approved consent: the request signature does not cover this request"
    assert world.db.payments == {} and world.db.balance("acct-ada") == 50_000


def test_a_missing_or_foreign_signature_is_refused(world):
    consent = grant(world)
    request = PaymentRequest(consent.consent_id, "acct-shop", Money(3000, "USD"))
    with pytest.raises(PaymentRefusedError) as missing:
        world.bank.initiate_payment.execute("merchant-1", IdempotencyKey("k-1"), request, None)
    forged = sign_request(
        world.other_merchant, consent.consent_id, "acct-shop", Money(3000, "USD"), "k-1", "merchant-1"
    )
    with pytest.raises(PaymentRefusedError) as foreign:
        world.bank.initiate_payment.execute("merchant-1", IdempotencyKey("k-1"), request, forged)
    assert missing.value.refusal == foreign.value.refusal == "request_not_as_signed"


def test_a_signature_does_not_move_to_another_attempt(world):
    consent = grant(world)
    signature = sign_request(world.merchant, consent.consent_id, "acct-shop", Money(3000, "USD"), "k-1")
    request = PaymentRequest(consent.consent_id, "acct-shop", Money(3000, "USD"))
    with pytest.raises(PaymentRefusedError, match="signature"):
        world.bank.initiate_payment.execute("merchant-1", IdempotencyKey("k-2"), request, signature)


def test_another_merchants_consent_is_refused_even_though_it_exists_and_is_valid(world):
    consent = grant(world)
    error = refused(world, consent.consent_id, 3000, "k-1", merchant=world.other_merchant)
    assert error.refusal == "consent_not_granted_to_caller"
    assert world.db.consents[consent.consent_id].used_at is None


def test_an_unknown_consent_is_not_found(world):
    error = refused(world, "00000000-0000-4000-8000-0000000000ff", 3000, "k-1")
    assert (error.refusal, error.message) == ("consent_not_found", "consent not found")


def test_a_revoked_consent_moves_no_money(world):
    consent = grant(world)
    world.bank.revoke_consent.execute("ada", world.ada_pin, consent.consent_id)
    error = refused(world, consent.consent_id, 3000, "k-1")
    assert (error.refusal, error.message) == ("consent_revoked", "consent revoked")
    assert world.db.balance("acct-ada") == 50_000


def test_a_single_payment_consent_pays_once(world):
    consent = grant(world)
    pay(world, consent.consent_id, 3000, "k-1")
    assert refused(world, consent.consent_id, 1000, "k-2").refusal == "consent_already_used"


def test_the_payer_cannot_be_overdrawn(world):
    consent = grant(world, terms(maximum=90_000))
    error = refused(world, consent.consent_id, 60_000, "k-1")
    assert error.refusal == "insufficient_funds" and world.db.balance("acct-ada") == 50_000


def test_a_recurring_consent_enforces_its_cap_over_the_period_and_then_the_window_moves(world):
    consent = grant(world, terms(maximum=10_000, scope=ConsentScope.RECURRING, period=timedelta(days=30)))
    pay(world, consent.consent_id, 6000, "r-1")
    error = refused(world, consent.consent_id, 5000, "r-2")
    assert error.refusal == "period_cap_exceeded" and "110.00" in error.message
    pay(world, consent.consent_id, 4000, "r-3")  # exactly the cap
    world.clock.advance(timedelta(days=31))
    pay(world, consent.consent_id, 5000, "r-4")
    assert world.db.balance("acct-ada") == 50_000 - 6000 - 4000 - 5000 and world.db.ledger_sum() == 0


def test_twenty_concurrent_requests_with_one_key_pay_once(world):
    consent = grant(world)
    results = []
    barrier = threading.Barrier(20)

    def call():
        barrier.wait()
        results.append(pay(world, consent.consent_id, 3000, "k-race"))

    threads = [threading.Thread(target=call) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(response == results[0][0] for response, _ in results)
    assert sum(1 for _, replayed in results if not replayed) == 1
    assert len(world.db.payments) == 1 and world.db.balance("acct-ada") == 47_000


def test_a_payment_is_read_back_only_by_the_merchant_that_made_it(world):
    consent = grant(world)
    response, _ = pay(world, consent.consent_id, 3000, "k-1")
    payment_id = response.body["payment_id"]
    assert world.bank.get_payment.execute("merchant-1", payment_id)["amount_minor"] == 3000
    with pytest.raises(PaymentNotFoundError):
        world.bank.get_payment.execute("merchant-2", payment_id)
    with pytest.raises(PaymentNotFoundError):
        world.bank.get_payment.execute("merchant-1", "00000000-0000-4000-8000-0000000000ff")


def test_a_payment_from_an_unregistered_caller_is_refused(world):
    consent = grant(world)
    request = PaymentRequest(consent.consent_id, "acct-shop", Money(3000, "USD"))
    with pytest.raises(ClientAuthenticationError):
        world.bank.initiate_payment.execute("merchant-9", IdempotencyKey("k-1"), request, "sig")


def test_a_failure_while_posting_rolls_back_the_payment_and_the_key(world, monkeypatch):
    consent = grant(world)

    def explode(*args, **kwargs):
        raise RuntimeError("connection reset")

    with world.db.unit_of_work() as uow, monkeypatch.context() as patch:
        patch.setattr(type(uow.payments), "add", explode)
        with pytest.raises(RuntimeError):
            pay(world, consent.consent_id, 3000, "k-1")
    assert world.db.balance("acct-ada") == 50_000 and world.db.keys == {} and world.db.payments == {}
    response, replayed = pay(world, consent.consent_id, 3000, "k-1")
    assert response.status == 201 and not replayed


def test_balances_and_the_ledger_sum_are_readable(world):
    balances, total = world.bank.balances.execute(["acct-ada", "acct-shop"])
    assert (balances["acct-ada"].minor, balances["acct-shop"].minor, total.minor) == (50_000, 0, 0)
    assert world.bank.balances.counts().consents == 0
    from open_banking.domain.errors import AccountNotFoundError

    with pytest.raises(AccountNotFoundError):
        world.bank.balances.execute(["acct-nowhere"])


OPERATIONS = st.sampled_from(["grant", "pay", "pay_again", "revoke", "tick", "tamper", "other_merchant"])


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture]
)
@given(steps=st.lists(st.tuples(OPERATIONS, st.integers(min_value=1, max_value=70_000)), max_size=25))
def test_no_sequence_of_operations_overdraws_an_account_creates_money_or_pays_without_a_consent(steps):
    from tests.unit.conftest import build_world

    world = build_world()
    consent_ids: list[str] = []
    for index, (operation, amount) in enumerate(steps):
        try:
            if operation == "grant":
                consent_ids.append(grant(world, terms(maximum=amount)).consent_id)
            elif consent_ids and operation in {"pay", "pay_again"}:
                consent_id = consent_ids[amount % len(consent_ids)]
                key = f"k-{index}" if operation == "pay" else f"k-{amount % 5}"
                pay(world, consent_id, amount, key)
            elif consent_ids and operation == "revoke":
                world.bank.revoke_consent.execute("ada", world.ada_pin, consent_ids[amount % len(consent_ids)])
            elif operation == "tick":
                world.clock.advance(timedelta(minutes=amount % 15))
            elif consent_ids and operation == "tamper":
                pay(world, consent_ids[0], amount, f"t-{index}", signed_minor=amount + 1)
            elif consent_ids and operation == "other_merchant":
                pay(world, consent_ids[0], amount, f"o-{index}", merchant=world.other_merchant)
        except (PaymentRefusedError, IdempotencyKeyReusedError):
            pass
        assert world.db.ledger_sum() == 0
        assert world.db.balance("acct-ada") >= 0
    assert world.db.balance("acct-ada") + world.db.balance("acct-shop") == 50_000
    for payment in world.db.payments.values():
        consent = world.db.consents[payment.consent_id]
        assert payment.amount.minor <= consent.terms.max_amount.minor
        assert payment.created_at < consent.expires_at
        assert payment.client_id == consent.terms.client_id
        assert consent.revoked_at is None or payment.created_at <= consent.revoked_at
    assert len({UUID(p.payment_id) for p in world.db.payments.values()}) == len(world.db.payments)
