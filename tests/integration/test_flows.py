"""The lesson's rules against the real database: authentication, consent, payments, and what happens under races."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from open_banking.domain.consent import ConsentScope
from open_banking.domain.errors import IdempotencyKeyReusedError, InvalidProofError, PaymentRefusedError
from tests.integration.conftest import Flow
from tests.unit.helpers import grant, pay, terms

pytestmark = pytest.mark.integration


def balance(flow: Flow, account: str) -> int:
    balances, _ = flow.bank.balances.execute([account])
    return balances[account].minor


def ledger_sum(flow: Flow) -> int:
    return flow.bank.balances.execute(["acct-ada"])[1].minor


def attempt(flow: Flow, consent_id: str, amount: int, key: str, **kwargs):
    try:
        return pay(flow, consent_id, amount, key, **kwargs)[0].status
    except PaymentRefusedError as error:
        return error.refusal


def test_a_consent_comes_only_from_a_proof_and_a_payment_moves_exactly_the_money(flow):
    with pytest.raises(InvalidProofError):
        flow.bank.grant_consent.execute("not-a-proof", terms())
    consent = grant(flow)
    response, replayed = pay(flow, consent.consent_id, 3000, "k-1")
    assert response.status == 201 and not replayed
    assert (balance(flow, "acct-ada"), balance(flow, "acct-shop"), ledger_sum(flow)) == (47_000, 3000, 0)


def test_a_replay_is_a_no_op_and_a_changed_request_under_the_same_key_is_refused(flow):
    consent = grant(flow, terms(maximum=10_000, scope=ConsentScope.RECURRING, period=timedelta(days=30)))
    first, _ = pay(flow, consent.consent_id, 3000, "k-1")
    second, replayed = pay(flow, consent.consent_id, 3000, "k-1")
    assert replayed and first == second and balance(flow, "acct-ada") == 47_000
    with pytest.raises(IdempotencyKeyReusedError):
        pay(flow, consent.consent_id, 3100, "k-1")


def test_each_refusal_has_its_own_reason_against_the_real_database(flow):
    consent = grant(flow)
    assert attempt(flow, consent.consent_id, 6000, "k-a") == "amount_exceeds_consent"
    assert attempt(flow, consent.consent_id, 3000, "k-b", payee="acct-other-shop") == "payee_not_covered"
    assert (
        attempt(flow, consent.consent_id, 3000, "k-c", merchant=flow.other_merchant) == "consent_not_granted_to_caller"
    )
    assert attempt(flow, consent.consent_id, 3500, "k-d", signed_minor=3000) == "request_not_as_signed"
    assert attempt(flow, "00000000-0000-4000-8000-0000000000ff", 3000, "k-e") == "consent_not_found"
    flow.clock.advance(timedelta(minutes=10, seconds=1))
    assert attempt(flow, consent.consent_id, 3000, "k-f") == "consent_expired"
    revoked = grant(flow)
    flow.bank.revoke_consent.execute("ada", flow.ada_pin, revoked.consent_id)
    assert attempt(flow, revoked.consent_id, 3000, "k-g") == "consent_revoked"
    assert balance(flow, "acct-ada") == 50_000 and ledger_sum(flow) == 0


def test_twenty_concurrent_requests_with_one_key_pay_once(flow):
    consent = grant(flow)
    barrier = threading.Barrier(20)

    def call():
        barrier.wait(timeout=30)
        return pay(flow, consent.consent_id, 3000, "k-race")

    with ThreadPoolExecutor(max_workers=20) as workers:
        results = [future.result(timeout=120) for future in [workers.submit(call) for _ in range(20)]]
    assert all(response == results[0][0] for response, _ in results)
    assert sum(1 for _, replayed in results if not replayed) == 1
    assert balance(flow, "acct-ada") == 47_000 and ledger_sum(flow) == 0


def test_twenty_different_keys_against_one_single_payment_consent_pay_once(flow):
    consent = grant(flow)
    barrier = threading.Barrier(20)

    def call(index: int):
        barrier.wait(timeout=30)
        return attempt(flow, consent.consent_id, 1000, f"k-{index}")

    with ThreadPoolExecutor(max_workers=20) as workers:
        outcomes = [future.result(timeout=120) for future in [workers.submit(call, i) for i in range(20)]]
    assert sorted(map(str, outcomes)) == ["201"] + ["consent_already_used"] * 19
    assert balance(flow, "acct-ada") == 49_000


def test_concurrent_payments_never_overdraw_the_payer(flow):
    consent = grant(flow, terms(maximum=10_000, scope=ConsentScope.RECURRING, period=timedelta(days=365)))
    barrier = threading.Barrier(20)

    def call(index: int):
        barrier.wait(timeout=30)
        return attempt(flow, consent.consent_id, 10_000, f"k-{index}")

    # 20 payments of 100.00 under a cap of 100.00 per period: the cap lets one through; ada keeps the rest.
    with ThreadPoolExecutor(max_workers=20) as workers:
        outcomes = [future.result(timeout=120) for future in [workers.submit(call, i) for i in range(20)]]
    assert sorted(map(str, outcomes)) == ["201"] + ["period_cap_exceeded"] * 19
    assert balance(flow, "acct-ada") == 40_000 and ledger_sum(flow) == 0


def test_payments_in_opposite_directions_do_not_deadlock(flow):
    forward = grant(flow, terms(payee="acct-bob", maximum=1000, scope=ConsentScope.RECURRING, period=timedelta(days=1)))
    backward = grant(
        flow,
        terms(payer="acct-bob", payee="acct-ada", maximum=1000, scope=ConsentScope.RECURRING, period=timedelta(days=1)),
        "bob",
    )
    barrier = threading.Barrier(16)

    def call(index: int):
        barrier.wait(timeout=30)
        if index % 2:
            return attempt(flow, forward.consent_id, 10, f"f-{index}", payee="acct-bob")
        return attempt(flow, backward.consent_id, 10, f"b-{index}", payee="acct-ada")

    with ThreadPoolExecutor(max_workers=16) as workers:
        outcomes = [future.result(timeout=120) for future in [workers.submit(call, i) for i in range(16)]]
    assert set(outcomes) == {201}
    assert balance(flow, "acct-ada") + balance(flow, "acct-bob") == 70_000 and ledger_sum(flow) == 0


@pytest.mark.parametrize("round_number", range(6))
def test_a_payment_and_a_revocation_at_the_same_moment_never_pay_after_the_revocation(flow, connection, round_number):
    consent = grant(flow, terms(maximum=500))
    barrier = threading.Barrier(2)

    def do_pay():
        barrier.wait(timeout=30)
        return attempt(flow, consent.consent_id, 100, f"race-{uuid.uuid4().hex[:6]}")

    def do_revoke():
        barrier.wait(timeout=30)
        return flow.bank.revoke_consent.execute("ada", flow.ada_pin, consent.consent_id)

    with ThreadPoolExecutor(max_workers=2) as workers:
        paid, revoked = workers.submit(do_pay), workers.submit(do_revoke)
        outcome, revocation = paid.result(timeout=60), revoked.result(timeout=60)
    assert revocation.revoked_at is not None
    payments = connection.execute("SELECT created_at FROM payments").fetchall()
    connection.rollback()
    if outcome == 201:
        assert len(payments) == 1 and payments[0][0] <= revocation.revoked_at
        assert balance(flow, "acct-ada") == 49_900
    else:
        assert outcome == "consent_revoked" and payments == [] and balance(flow, "acct-ada") == 50_000


def test_a_failure_while_posting_rolls_back_the_payment_and_the_key_together(flow, monkeypatch):
    from open_banking.infrastructure.postgres import PostgresPayments

    consent = grant(flow)

    def explode(self, payment):
        raise RuntimeError("connection reset")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresPayments, "add", explode)
        with pytest.raises(RuntimeError):
            pay(flow, consent.consent_id, 3000, "k-1")
    assert balance(flow, "acct-ada") == 50_000 and ledger_sum(flow) == 0
    response, replayed = pay(flow, consent.consent_id, 3000, "k-1")  # nothing was remembered, so the same key works
    assert response.status == 201 and not replayed


def test_the_health_probe_reports_the_database(flow):
    assert flow.bank.health.execute() == {"database": "ok"}
