from datetime import timedelta

import pytest

from open_banking.domain.authentication import MAX_ATTEMPTS, AttemptOutcome, Challenge, ChallengeStatus
from open_banking.domain.consent import (
    AuthenticationProof,
    Consent,
    ConsentScope,
    ConsentTerms,
    PaymentRequest,
    Refusal,
)
from open_banking.domain.errors import InvalidProofError
from open_banking.domain.money import Money
from tests.unit.fakes import START
from tests.unit.helpers import terms

USD = "USD"
TTL = timedelta(minutes=10)


def proof_for(consent_terms: ConsentTerms, **overrides) -> AuthenticationProof:
    values = {
        "proof_id": "p1",
        "customer_id": "c1",
        "terms_digest": consent_terms.digest(),
        "expires_at": START + timedelta(minutes=2),
    }
    return AuthenticationProof(**{**values, **overrides})


def consent(consent_terms: ConsentTerms | None = None) -> Consent:
    consent_terms = consent_terms or terms()
    return Consent.grant("k1", proof_for(consent_terms), consent_terms, START, TTL)


def request(amount: int = 3000, payee: str = "acct-shop") -> PaymentRequest:
    return PaymentRequest("k1", payee, Money(amount, USD))


def test_a_consent_is_created_from_a_valid_proof_and_carries_its_scope_and_expiry():
    granted = consent()
    assert granted.terms.scope is ConsentScope.SINGLE_PAYMENT
    assert granted.expires_at == START + TTL and granted.customer_id == "c1"
    assert granted.revoked_at is None and granted.used_at is None


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"consumed": True}, "already used"),
        ({"expires_at": START}, "expired"),
        ({"terms_digest": "0" * 64}, "different terms"),
    ],
)
def test_a_consent_cannot_be_created_from_a_proof_that_is_spent_expired_or_for_other_terms(override, message):
    consent_terms = terms()
    with pytest.raises(InvalidProofError, match=message):
        Consent.grant("k1", proof_for(consent_terms, **override), consent_terms, START, TTL)


def test_terms_need_a_positive_maximum_and_a_period_exactly_for_recurring_consents():
    with pytest.raises(InvalidProofError):
        terms(maximum=0)
    with pytest.raises(InvalidProofError):
        terms(scope=ConsentScope.RECURRING)
    with pytest.raises(InvalidProofError):
        terms(period=timedelta(days=30))
    assert terms(scope=ConsentScope.RECURRING, period=timedelta(days=30)).period == timedelta(days=30)


def test_the_digest_changes_with_every_term_so_a_proof_cannot_be_moved_to_other_terms():
    base = terms().digest()
    variants = [
        terms(payer="acct-bob"),
        terms(payee="acct-other-shop"),
        terms(client="merchant-2"),
        terms(maximum=5001),
        terms(scope=ConsentScope.RECURRING, period=timedelta(days=1)),
        ConsentTerms("acct-ada", "acct-shop", "merchant-1", Money(5000, "EUR"), ConsentScope.SINGLE_PAYMENT),
    ]
    assert len({base, *(variant.digest() for variant in variants)}) == len(variants) + 1
    assert terms().digest() == base


def test_revoking_sets_the_time_once_and_using_marks_it_used():
    first = consent().revoked(START + timedelta(minutes=1))
    assert first.revoked_at == START + timedelta(minutes=1)
    assert first.revoked(START + timedelta(minutes=5)) is first
    assert consent().marked_used(START).used_at == START


def refusal(granted: Consent, req: PaymentRequest, *, caller="merchant-1", at=START, spent=0) -> Refusal | None:
    return granted.refusal_for(req, caller, at, Money(spent, USD))


def test_a_payment_within_the_consent_has_no_refusal():
    assert refusal(consent(), request()) is None
    assert refusal(consent(), request(5000)) is None  # exactly the maximum is allowed


def test_each_refusal_has_its_own_reason_and_message():
    granted = consent()
    cases = [
        (
            refusal(granted, request(), caller="merchant-2"),
            Refusal.NOT_GRANTED_TO_CALLER,
            "consent was not granted to this merchant",
        ),
        (
            refusal(granted, request(payee="acct-other-shop")),
            Refusal.PAYEE_NOT_COVERED,
            "consent does not cover this payee",
        ),
        (refusal(granted.revoked(START), request()), Refusal.CONSENT_REVOKED, "consent revoked"),
        (refusal(granted, request(), at=START + TTL), Refusal.CONSENT_EXPIRED, "consent expired"),
        (
            refusal(granted.marked_used(START), request()),
            Refusal.CONSENT_ALREADY_USED,
            "single-payment consent already used",
        ),
        (refusal(granted, request(6000)), Refusal.AMOUNT_EXCEEDS_CONSENT, "amount 60.00 exceeds consent max 50.00"),
    ]
    for found, expected, message in cases:
        assert found is expected
        spent = Money(0, USD)
        assert (
            granted.explain(found, request(6000 if expected is Refusal.AMOUNT_EXCEEDS_CONSENT else 3000), spent)
            == message
        )


def test_the_consent_is_valid_up_to_but_not_including_its_expiry():
    granted = consent()
    assert refusal(granted, request(), at=START + TTL - timedelta(seconds=1)) is None
    assert refusal(granted, request(), at=START + TTL) is Refusal.CONSENT_EXPIRED


def test_a_different_currency_is_not_covered():
    foreign = PaymentRequest("k1", "acct-shop", Money(3000, "EUR"))
    assert refusal(consent(), foreign) is Refusal.CURRENCY_NOT_COVERED
    assert "USD" in consent().explain(Refusal.CURRENCY_NOT_COVERED, foreign, Money(0, USD))


def test_the_checks_run_in_a_fixed_order_caller_payee_revoked_expired_used_amount():
    worst = consent().revoked(START).marked_used(START)
    assert (
        refusal(worst, request(6000, payee="acct-other-shop"), caller="merchant-2", at=START + TTL)
        is Refusal.NOT_GRANTED_TO_CALLER
    )
    assert refusal(worst, request(6000, payee="acct-other-shop"), at=START + TTL) is Refusal.PAYEE_NOT_COVERED
    assert refusal(worst, request(6000), at=START + TTL) is Refusal.CONSENT_REVOKED
    assert refusal(consent().marked_used(START), request(6000), at=START + TTL) is Refusal.CONSENT_EXPIRED
    assert refusal(consent().marked_used(START), request(6000)) is Refusal.CONSENT_ALREADY_USED


def test_a_recurring_consent_caps_the_total_in_its_period_and_is_not_used_up():
    recurring = consent(terms(maximum=10_000, scope=ConsentScope.RECURRING, period=timedelta(days=30)))
    used = recurring.marked_used(START)
    assert refusal(used, request(6000), spent=0) is None
    assert refusal(used, request(5000), spent=6000) is Refusal.PERIOD_CAP_EXCEEDED
    assert refusal(used, request(4000), spent=6000) is None
    assert "total to 110.00, over the consent cap 100.00" in used.explain(
        Refusal.PERIOD_CAP_EXCEEDED, request(5000), Money(6000, USD)
    )


def test_the_remaining_messages():
    granted = consent()
    spent = Money(0, USD)
    assert granted.explain(Refusal.CONSENT_NOT_FOUND, request(), spent) == "consent not found"
    assert "amount does not match approved consent" in granted.explain(Refusal.REQUEST_NOT_AS_SIGNED, request(), spent)
    assert granted.explain(Refusal.INSUFFICIENT_FUNDS, request(), spent) == "the payer has insufficient funds"


def challenge() -> Challenge:
    return Challenge("c1", "u1", "t" * 64, "d" * 64, START, START + timedelta(minutes=5))


def test_the_right_code_verifies_the_challenge():
    updated, outcome = challenge().attempt(True, START)
    assert outcome is AttemptOutcome.VERIFIED and updated.status is ChallengeStatus.VERIFIED


def test_wrong_codes_count_and_the_third_locks_the_challenge():
    current = challenge()
    for expected_attempts in range(1, MAX_ATTEMPTS):
        current, outcome = current.attempt(False, START)
        assert (outcome, current.attempts) == (AttemptOutcome.WRONG_CODE, expected_attempts)
    current, outcome = current.attempt(False, START)
    assert outcome is AttemptOutcome.LOCKED and current.status is ChallengeStatus.LOCKED
    again, outcome = current.attempt(True, START)  # even the right code is refused once locked
    assert outcome is AttemptOutcome.LOCKED and again is current


def test_an_expired_or_used_challenge_accepts_nothing():
    assert challenge().attempt(True, START + timedelta(minutes=5))[1] is AttemptOutcome.EXPIRED
    verified, _ = challenge().attempt(True, START)
    assert verified.attempt(True, START)[1] is AttemptOutcome.ALREADY_USED
