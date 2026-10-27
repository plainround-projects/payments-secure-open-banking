"""The lesson's step 2: a consent exists only after strong customer authentication, with a scope and an expiry."""

from datetime import timedelta

import pytest

from open_banking.domain.authentication import AttemptOutcome
from open_banking.domain.consent import ConsentScope
from open_banking.domain.errors import (
    AccountNotFoundError,
    AccountNotOwnedError,
    AuthenticationFailedError,
    ChallengeNotFoundError,
    ConsentNotFoundError,
    InvalidProofError,
    UnknownClientError,
)
from tests.unit.helpers import authenticate, grant, terms


def test_a_consent_exists_only_after_the_customer_passed_both_factors(world):
    consent = grant(world)
    assert consent.terms.scope is ConsentScope.SINGLE_PAYMENT
    assert consent.expires_at == world.clock.now() + timedelta(minutes=10)
    assert world.db.consents[consent.consent_id] is consent


def test_a_consent_cannot_be_created_without_a_proof_or_with_a_made_up_one(world):
    with pytest.raises(InvalidProofError, match="not known"):
        world.bank.grant_consent.execute("not-a-proof", terms())
    assert world.db.consents == {}


def test_the_code_is_sent_to_the_phone_and_never_stored_in_clear(world):
    world.bank.start_challenge.execute("ada", world.ada_pin, terms())
    [(_, code, _)] = world.phone.messages
    stored = next(iter(world.db.challenges.values()))
    assert code not in stored.code_digest and len(stored.code_digest) == 64
    assert code not in repr(stored)


def test_a_wrong_pin_and_an_unknown_user_get_the_same_answer(world):
    for username, pin in (("ada", "000000"), ("nobody", world.ada_pin)):
        with pytest.raises(AuthenticationFailedError, match="the username or PIN is not right"):
            world.bank.start_challenge.execute(username, pin, terms())
    assert world.phone.messages == [] and world.db.challenges == {}


def test_abandoned_authentication_leaves_no_consent(world):
    started = world.bank.start_challenge.execute("ada", world.ada_pin, terms())
    assert started.challenge_id in world.db.challenges
    assert world.db.consents == {} and world.db.proofs == {}


def test_three_wrong_codes_lock_the_challenge_and_even_the_right_code_is_then_refused(world):
    started = world.bank.start_challenge.execute("ada", world.ada_pin, terms())
    results = [world.bank.verify_challenge.execute(started.challenge_id, "000000") for _ in range(3)]
    assert [r.outcome for r in results] == [AttemptOutcome.WRONG_CODE, AttemptOutcome.WRONG_CODE, AttemptOutcome.LOCKED]
    assert [r.attempts_left for r in results] == [2, 1, 0]
    right = world.phone.latest_code(world.db.customers_by_name("ada").customer_id)
    assert world.bank.verify_challenge.execute(started.challenge_id, right).outcome is AttemptOutcome.LOCKED
    assert world.db.proofs == {}


def test_an_expired_challenge_and_a_challenge_used_twice_are_refused(world):
    started = world.bank.start_challenge.execute("ada", world.ada_pin, terms())
    code = world.phone.latest_code(world.db.customers_by_name("ada").customer_id)
    assert world.bank.verify_challenge.execute(started.challenge_id, code).outcome is AttemptOutcome.VERIFIED
    assert world.bank.verify_challenge.execute(started.challenge_id, code).outcome is AttemptOutcome.ALREADY_USED
    late = world.bank.start_challenge.execute("ada", world.ada_pin, terms())
    world.clock.advance(timedelta(minutes=5))
    code = world.phone.latest_code(world.db.customers_by_name("ada").customer_id)
    assert world.bank.verify_challenge.execute(late.challenge_id, code).outcome is AttemptOutcome.EXPIRED


def test_an_unknown_challenge_is_an_error(world):
    with pytest.raises(ChallengeNotFoundError):
        world.bank.verify_challenge.execute("00000000-0000-4000-8000-0000000000ff", "123456")


def test_a_proof_is_tied_to_the_terms_the_customer_saw(world):
    proof = authenticate(world, "ada", world.ada_pin, terms(maximum=5000))
    with pytest.raises(InvalidProofError, match="different terms"):
        world.bank.grant_consent.execute(proof, terms(maximum=500_000))
    assert world.db.consents == {}
    assert world.bank.grant_consent.execute(proof, terms(maximum=5000)).terms.max_amount.minor == 5000


def test_a_proof_is_good_for_one_consent_and_for_two_minutes(world):
    proof = authenticate(world, "ada", world.ada_pin, terms())
    world.bank.grant_consent.execute(proof, terms())
    with pytest.raises(InvalidProofError, match="already used"):
        world.bank.grant_consent.execute(proof, terms())
    stale = authenticate(world, "ada", world.ada_pin, terms())
    world.clock.advance(timedelta(minutes=2))
    with pytest.raises(InvalidProofError, match="expired"):
        world.bank.grant_consent.execute(stale, terms())


def test_a_customer_can_only_approve_payments_from_their_own_account(world):
    with pytest.raises(AccountNotOwnedError):
        world.bank.start_challenge.execute("ada", world.ada_pin, terms(payer="acct-bob"))


def test_the_merchant_and_the_payee_must_exist(world):
    with pytest.raises(UnknownClientError):
        world.bank.start_challenge.execute("ada", world.ada_pin, terms(client="merchant-9"))
    with pytest.raises(AccountNotFoundError):
        world.bank.start_challenge.execute("ada", world.ada_pin, terms(payee="acct-nowhere"))
    proof = authenticate(world, "ada", world.ada_pin, terms())
    del world.db.clients["merchant-1"]
    with pytest.raises(UnknownClientError):
        world.bank.grant_consent.execute(proof, terms())


def test_a_recurring_consent_lives_ninety_days_and_a_single_payment_ten_minutes(world):
    recurring = grant(world, terms(scope=ConsentScope.RECURRING, period=timedelta(days=30)))
    single = grant(world)
    assert recurring.expires_at - recurring.created_at == timedelta(days=90)
    assert single.expires_at - single.created_at == timedelta(minutes=10)


def test_the_customer_can_revoke_and_revoking_twice_changes_nothing(world):
    consent = grant(world)
    first = world.bank.revoke_consent.execute("ada", world.ada_pin, consent.consent_id)
    world.clock.advance(timedelta(minutes=1))
    second = world.bank.revoke_consent.execute("ada", world.ada_pin, consent.consent_id)
    assert first.revoked_at == second.revoked_at is not None


def test_only_the_owner_with_the_right_pin_can_revoke(world):
    consent = grant(world)
    with pytest.raises(AuthenticationFailedError):
        world.bank.revoke_consent.execute("ada", "000000", consent.consent_id)
    with pytest.raises(ConsentNotFoundError):
        world.bank.revoke_consent.execute("bob", world.bob_pin, consent.consent_id)
    with pytest.raises(ConsentNotFoundError):
        world.bank.revoke_consent.execute("ada", world.ada_pin, "00000000-0000-4000-8000-0000000000ff")
    assert world.db.consents[consent.consent_id].revoked_at is None


def test_enrolling_and_registering_refuse_unknown_accounts_and_store_only_hashes(world):
    with pytest.raises(AccountNotFoundError):
        world.bank.enroll_customer.execute("carol", "654321", "acct-nowhere")
    stored = world.db.customers_by_name("ada")
    assert world.ada_pin not in stored.pin_hash and stored.pin_hash.startswith("scrypt$")
    assert world.merchant.secret not in world.db.clients["merchant-1"].secret_hash
