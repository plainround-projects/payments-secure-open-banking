# pytest walkthrough: the integration and end-to-end tests, line by line

## What it is

This note reads every line of the tests that need the real PostgreSQL (`make up` first): the lesson's rules on real transactions (including twenty-thread races and a payment racing a revocation), the schema constraints, and the end-to-end tests that start the actual bank and merchant as separate processes and run the real programs.

An **integration test** checks that parts work together, here the use cases with the real database adapters. An **end-to-end test** drives the finished programs from the outside.

## Why this repository uses it

Some promises can only be shown with the real system: that twenty simultaneous requests with one key pay once (advisory locks), that twenty payments never overdraw an account (row locks), that opposite payments do not deadlock (ordered locking), that a payment and a revocation at the same instant are ordered consistently, that the database itself refuses a balance that disagrees with its postings, and that SIGTERM stops a server cleanly without leaking a secret into its log. Fakes cannot prove these.

## Where it appears

| File | What it is |
| --- | --- |
| `tests/integration/conftest.py` | Fixtures for the bank on PostgreSQL. |
| `tests/integration/test_flows.py` | The lesson's rules and races on PostgreSQL. |
| `tests/integration/test_schema_constraints.py` | The database refuses bad data. |
| `tests/e2e/conftest.py` | Fixtures that start real server processes. |
| `tests/e2e/test_lesson_scenario.py` | The lesson through real processes. |

## How it works underneath

Each integration test starts from a freshly loaded schema and seed. Tests talk to PostgreSQL through psycopg. Concurrency tests start threads that wait on a `Barrier` so all start at the same instant. The end-to-end fixture runs the admin program to create secrets, starts `serve_bank` and `serve_merchant` as processes on free ports, waits for `/health`, drives them with the customer program and HTTP, and stops them with SIGTERM.

## Walkthrough of this repository's use

Fixtures first, then the tests.

### `tests/integration/conftest.py` (97 lines)

```python
"""Fixtures for the integration tests: the bank on PostgreSQL with real cryptography and a fresh schema per test."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest

from open_banking.application.bank.ports import Customer
from open_banking.infrastructure import keys, postgres
from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.crypto import StandardCrypto
from open_banking.infrastructure.otp import MemoryOtpChannel
from open_banking.infrastructure.system import OffsetClock, RandomIds
from open_banking.interfaces.bank_container import BankContainer, assemble_bank
from tests.conftest import load_schema, wait_until_ready
from tests.unit.conftest import Merchant
```

Fixtures for the integration tests: the bank on PostgreSQL with real cryptography and a fresh schema per test.

```python
@pytest.fixture(autouse=True)
def _schema(settings: BankConfig) -> None:
    """Every test starts from a freshly loaded schema and seed, so no test sees another's payments."""
    wait_until_ready(settings)
    with psycopg.connect(postgres.conninfo(settings.postgres), autocommit=True) as admin:
        load_schema(admin)
```

`@pytest.fixture(autouse=True)` above it means every test in this folder uses the fixture without asking for it. Each test starts from a freshly loaded schema and seed, so no test sees another's payments: wait until the database is ready, then connect as an administrator (autocommit) and run `load_schema`.

```python
@pytest.fixture
def connection(settings: BankConfig) -> Iterator[psycopg.Connection[Any]]:
    """A plain connection (autocommit off) for tests that write SQL directly."""
    with psycopg.connect(postgres.conninfo(settings.postgres)) as connection:
        yield connection
```

A plain psycopg connection (autocommit off, so a test controls commits) for tests that write SQL directly. `with ... as connection` closes it at the end; `yield connection` hands it to the test.

```python
class DbView:
    """What the shared test helpers ask of a database: find a customer by name."""

    def __init__(self, bank: BankContainer) -> None:
        self._bank = bank

    def customers_by_name(self, username: str) -> Customer:
        with self._bank.uow_factory() as uow:
            customer = uow.customers.by_username(username)
        assert customer is not None
        return customer
```

What the shared test helpers ask of a database: find a customer by name. The helpers written for the unit tests call `world.db.customers_by_name(...)`; this small class answers the same question through the real PostgreSQL adapter (opening a transaction, reading, asserting the customer exists).

```python
@dataclass
class Flow:
    """The bank on PostgreSQL, shaped like the unit tests' `World` so the shared helpers work on both."""

    bank: BankContainer
    db: DbView
    clock: OffsetClock
    phone: MemoryOtpChannel
    merchant: Merchant
    other_merchant: Merchant
    ada_pin: str = "246810"
    bob_pin: str = "135791"
```

The bank on PostgreSQL, shaped like the unit tests' `World` so the shared helpers work on both: the bank container, the `DbView`, the offset clock, the in-memory phone, two merchants and the two PINs.

```python
def _register(bank: BankContainer, client_id: str) -> Merchant:
    pem, jwk = keys.generate_keypair()
    secret = f"secret-of-{client_id}-0123456789"
    bank.register_client.execute(client_id, client_id, secret, jwk)
    return Merchant(client_id, secret, pem, jwk)
```

Registers one merchant through the real use case with a fresh key pair and a secret.

```python
@pytest.fixture
def flow(settings: BankConfig) -> Iterator[Flow]:
    from tests.unit.conftest import default_settings

    pool = postgres.open_pool(settings.postgres)
    ids = RandomIds()
    clock = OffsetClock()
    phone = MemoryOtpChannel()
    crypto = StandardCrypto(pepper="integration-pepper-0123456789", scrypt_n=16)
    bank = assemble_bank(
        uow_factory=postgres.PostgresUnitOfWorkFactory(pool, ids.new_id),
        crypto=crypto,
        otp=phone,
        clock=clock,
        ids=ids,
        settings=default_settings(),
        public_url="http://bank.test",
        signing_pem=keys.generate_keypair()[0],
        probes={"database": postgres.PostgresHealth(pool)},
        close=pool.close,
    )
    flow = Flow(bank, DbView(bank), clock, phone, _register(bank, "merchant-1"), _register(bank, "merchant-2"))
    bank.enroll_customer.execute("ada", flow.ada_pin, "acct-ada")
    bank.enroll_customer.execute("bob", flow.bob_pin, "acct-bob")
    yield flow
    pool.close()
```

Builds the whole bank on PostgreSQL: a pool, random ids, an `OffsetClock`, the in-memory phone, real `StandardCrypto` with a tiny scrypt cost, and `assemble_bank` with the PostgreSQL unit-of-work factory, a database health probe and `pool.close`. It registers two merchants and enrolls Ada and Bob. After the test (`yield flow` gives control to the test; the line after the yield runs when it ends) the pool is closed. The import inside the function avoids a circular import.

### `tests/integration/test_flows.py` (194 lines)

```python
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
```

The lesson's rules against the real database: authentication, consent, payments, and what happens under races. `threading.Barrier` releases threads together; `ThreadPoolExecutor` runs functions on a pool of threads and gives back their results.

```python
pytestmark = pytest.mark.integration
```

Marks every test in the file as `integration`; the Makefile selects them with `-m integration`.

```python
def balance(flow: Flow, account: str) -> int:
    balances, _ = flow.bank.balances.execute([account])
    return balances[account].minor
```

Helper: an account's balance in minor units, read through the balances use case.

```python
def ledger_sum(flow: Flow) -> int:
    return flow.bank.balances.execute(["acct-ada"])[1].minor
```

Helper: the sum of every posting.

```python
def attempt(flow: Flow, consent_id: str, amount: int, key: str, **kwargs):
    try:
        return pay(flow, consent_id, amount, key, **kwargs)[0].status
    except PaymentRefusedError as error:
        return error.refusal
```

Helper: make a payment and return its status (201) or, if it was refused, the refusal code, so a test can compare simple values.

```python
def test_a_consent_comes_only_from_a_proof_and_a_payment_moves_exactly_the_money(flow):
    with pytest.raises(InvalidProofError):
        flow.bank.grant_consent.execute("not-a-proof", terms())
    consent = grant(flow)
    response, replayed = pay(flow, consent.consent_id, 3000, "k-1")
    assert response.status == 201 and not replayed
    assert (balance(flow, "acct-ada"), balance(flow, "acct-shop"), ledger_sum(flow)) == (47_000, 3000, 0)
```

A made-up proof cannot create a consent; a proper consent and a 30.00 payment leave Ada with 470.00, the shop with 30.00 and the ledger at zero.

```python
def test_a_replay_is_a_no_op_and_a_changed_request_under_the_same_key_is_refused(flow):
    consent = grant(flow, terms(maximum=10_000, scope=ConsentScope.RECURRING, period=timedelta(days=30)))
    first, _ = pay(flow, consent.consent_id, 3000, "k-1")
    second, replayed = pay(flow, consent.consent_id, 3000, "k-1")
    assert replayed and first == second and balance(flow, "acct-ada") == 47_000
    with pytest.raises(IdempotencyKeyReusedError):
        pay(flow, consent.consent_id, 3100, "k-1")
```

The same request twice is a replay with an equal answer and Ada debited once; the same key with 31.00 raises `IdempotencyKeyReusedError`.

```python
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
```

Seven refusals, each with its own code: over the amount, another payee, another merchant, a changed amount, an unknown consent, an expired consent (after the clock advances) and a revoked consent. Ada's balance is still 500.00 and the ledger sums to zero.

```python
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
```

Twenty threads call with one key; the barrier releases them together (each waits at most 30 seconds). All answers are equal, exactly one is not a replay, Ada is debited once. This depends on the advisory lock on (merchant, key).

```python
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
```

Twenty threads with twenty different keys against one single-payment consent: exactly one gets 201 and the other nineteen get `consent_already_used` (the consent row is locked with `FOR UPDATE`, so they take turns). The outcomes are sorted as texts to compare with a list.

```python
def test_concurrent_payments_never_overdraw_the_payer(flow):
    consent = grant(flow, terms(maximum=90_000, scope=ConsentScope.RECURRING, period=timedelta(days=365)))
    barrier = threading.Barrier(20)

    def call(index: int):
        barrier.wait(timeout=30)
        return attempt(flow, consent.consent_id, 10_000, f"k-{index}")

    # Ada has 500.00 and the cap allows 900.00: twenty payments of 100.00 can only be funded five times.
    with ThreadPoolExecutor(max_workers=20) as workers:
        outcomes = [future.result(timeout=120) for future in [workers.submit(call, i) for i in range(20)]]
    assert sorted(map(str, outcomes)) == ["201"] * 5 + ["insufficient_funds"] * 15
    assert balance(flow, "acct-ada") == 0 and balance(flow, "acct-shop") == 50_000 and ledger_sum(flow) == 0
```

The comment explains the numbers: Ada has 500.00 and the consent allows 900.00, so twenty payments of 100.00 can be funded only five times. The result is five 201 and fifteen `insufficient_funds`; Ada ends at zero, the shop has 500.00 and the ledger sums to zero.

```python
def test_twenty_concurrent_payments_under_a_period_cap_never_exceed_it(flow):
    consent = grant(flow, terms(maximum=10_000, scope=ConsentScope.RECURRING, period=timedelta(days=365)))
    barrier = threading.Barrier(20)

    def call(index: int):
        barrier.wait(timeout=30)
        return attempt(flow, consent.consent_id, 10_000, f"c-{index}")

    with ThreadPoolExecutor(max_workers=20) as workers:
        outcomes = [future.result(timeout=120) for future in [workers.submit(call, i) for i in range(20)]]
    assert sorted(map(str, outcomes)) == ["201"] + ["period_cap_exceeded"] * 19
    assert balance(flow, "acct-ada") == 40_000 and ledger_sum(flow) == 0
```

A recurring consent with a cap of 100.00 per year and twenty simultaneous payments of 100.00: exactly one succeeds and nineteen get `period_cap_exceeded`.

```python
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
```

Two consents, one from Ada to Bob and one from Bob to Ada. Sixteen threads alternate between them. All sixteen succeed (a **deadlock**, two transactions waiting for each other forever, would time out), because accounts are always locked in sorted order. The two balances together still add up to 700.00 and the ledger sums to zero.

```python
@pytest.mark.parametrize("round_number", range(6))
```

Repeats the next test six times to give the race more chances to show up.

```python
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
```

A payment and a revocation are released at the same instant. The revocation always succeeds. If the payment got 201, there is exactly one payment row and it was created no later than the revocation time, and Ada paid 1.00; otherwise the payment was refused as `consent_revoked`, there is no payment row and the balance is untouched. Either way the order is consistent.

```python
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
```

`PostgresPayments.add` is replaced by a function that raises, for the duration of a `monkeypatch.context()` block. The payment raises `RuntimeError`; the balance and the ledger are untouched; and the same key then works (status 201, not a replay) because nothing was remembered.

```python
def test_the_health_probe_reports_the_database(flow):
    assert flow.bank.health.execute() == {"database": "ok"}
```

The health use case returns `{"database": "ok"}`.

### `tests/integration/test_schema_constraints.py` (273 lines)

```python
"""The database refuses what the domain never produces: an unbalanced entry, a balance that disagrees, an overdraft."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from psycopg import errors

from tests.conftest import SQL
```

The database refuses what the domain never produces: an unbalanced entry, a balance that disagrees with its postings, an overdraft. `psycopg.errors` has one exception class per PostgreSQL error kind.

```python
pytestmark = pytest.mark.integration
```

Marks every test here as `integration`.

```python
NOW = "2026-10-27T10:00:00Z"
```

A fixed timestamp text used for `created_at`.

```python
def entry(connection: psycopg.Connection[Any], reference: str | None = None) -> str:
    entry_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO journal_entries (id, kind, reference, created_at) VALUES (%s, 'PAYMENT', %s, %s)",
        (entry_id, reference or str(uuid.uuid4()), NOW),
    )
    return entry_id
```

Inserts a `PAYMENT` journal entry with a random id and a given or random reference and returns its id. `%s` marks a value placeholder: psycopg sends the values separately from the SQL text.

```python
def post(
    connection: psycopg.Connection[Any], entry_id: str, account: str, amount: int, *, move_balance: bool = True
) -> None:
    connection.execute(
        "INSERT INTO postings (entry_id, account_id, amount_minor, currency, created_at) "
        "VALUES (%s, %s, %s, 'USD', %s)",
        (entry_id, account, amount, NOW),
    )
    if move_balance:
        connection.execute(
            "UPDATE bank_accounts SET balance_minor = balance_minor + %s WHERE account_id = %s", (amount, account)
        )
```

Inserts one posting and, unless `move_balance` is false, adds the same amount to the account's balance (the way the application does it), so a test can leave out the balance move on purpose.

```python
def check_deferred_constraints_now(connection: psycopg.Connection[Any]) -> None:
    """Fire the checks that would run at COMMIT, then roll back so the test leaves nothing behind."""
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.rollback()
```

`SET CONSTRAINTS ALL IMMEDIATE` runs every deferred check right now, instead of at commit; then `rollback()` removes the test's rows.

```python
def test_a_balanced_transfer_passes_the_commit_check(connection):
    entry_id = entry(connection)
    post(connection, entry_id, "acct-ada", -1000)
    post(connection, entry_id, "acct-shop", 1000)
    check_deferred_constraints_now(connection)
```

A debit of 10.00 from Ada and a credit of 10.00 to the shop, with the balances moved, passes.

```python
@pytest.mark.parametrize(
    "rows",
    [
        [("acct-ada", -1000)],
        [("acct-ada", -1000), ("acct-shop", 900)],
        [("acct-ada", -1000), ("acct-ada", 1000)],
        [("acct-ada", -1000), ("acct-shop", 500), ("acct-bob", 500)],
        [],
    ],
)
```

Five bad entries: a single row, a sum that is off by 100, both rows on one account, three rows, and no rows at all.

```python
def test_an_entry_that_is_not_a_balanced_pair_cannot_commit(connection, rows):
    entry_id = entry(connection)
    for account, amount in rows:
        post(connection, entry_id, account, amount)
    with pytest.raises(errors.CheckViolation):
        connection.commit()
```

Each case inserts its rows, and `connection.commit()` raises `CheckViolation` because a deferred trigger fires at commit.

```python
def test_a_balance_that_moved_without_a_posting_cannot_commit(connection):
    connection.execute("UPDATE bank_accounts SET balance_minor = balance_minor + 100 WHERE account_id = 'acct-shop'")
    with pytest.raises(errors.CheckViolation, match="does not equal its postings"):
        connection.commit()
```

Adding 1.00 to the shop's balance with no posting fails at commit with `does not equal its postings`.

```python
def test_a_posting_without_the_balance_moving_cannot_commit(connection):
    entry_id = entry(connection)
    post(connection, entry_id, "acct-ada", -1000, move_balance=False)
    post(connection, entry_id, "acct-shop", 1000, move_balance=False)
    with pytest.raises(errors.CheckViolation, match="does not equal its postings"):
        connection.commit()
```

A balanced pair of postings with no balance change fails the same way: neither half can happen without the other.

```python
def test_a_customer_account_cannot_be_overdrawn_but_the_vault_may_be_negative(connection):
    entry_id = entry(connection)
    with pytest.raises(errors.CheckViolation, match="bank_accounts_not_overdrawn"):
        post(connection, entry_id, "acct-ada", -50_001)
    connection.rollback()
    assert connection.execute("SELECT balance_minor FROM bank_accounts WHERE account_id = 'acct-vault'").fetchone() == (
        -70_000,
    )
```

Debiting Ada by 500.01 (one cent more than she has) breaks `bank_accounts_not_overdrawn` immediately (the check is not deferred); the vault's balance is -70000, which is allowed.

```python
def test_the_ledger_cannot_be_edited_or_deleted(connection):
    for statement in (
        "UPDATE postings SET amount_minor = amount_minor + 1",
        "DELETE FROM postings",
        "DELETE FROM journal_entries",
        "UPDATE journal_entries SET reference = 'x'",
    ):
        with pytest.raises(errors.ObjectNotInPrerequisiteState, match="append-only"):
            connection.execute(statement)
        connection.rollback()
```

Four statements (update postings, delete postings, delete entries, update entries) each raise `ObjectNotInPrerequisiteState` with the message `append-only`; the loop rolls back after each failed statement.

```python
def test_a_posting_of_zero_and_a_repeated_reference_are_refused(connection):
    entry_id = entry(connection, "ref-1")
    with pytest.raises(errors.CheckViolation, match="postings_amount_nonzero"):
        post(connection, entry_id, "acct-ada", 0)
    connection.rollback()
    entry(connection, "ref-2")
    with pytest.raises(errors.UniqueViolation, match="journal_entries_once_per_reference"):
        entry(connection, "ref-2")
```

A zero posting breaks `postings_amount_nonzero`. A second entry with the same kind and reference violates `journal_entries_once_per_reference`.

```python
def test_idempotency_keys_are_scoped_to_the_merchant(connection):
    for client in ("merchant-1", "merchant-2"):
        connection.execute(
            "INSERT INTO oauth_clients (client_id, name, secret_hash, public_jwk) VALUES (%s, %s, 'h', '{}')",
            (client, client),
        )
    insert = (
        "INSERT INTO idempotency_keys (client_id, key, request_fingerprint, response_status, response_body, "
        "created_at, expires_at) VALUES (%s, 'same-key', 'f', 201, '{}', now(), now() + interval '1 day')"
    )
    connection.execute(insert, ("merchant-1",))
    connection.execute(insert, ("merchant-2",))  # another merchant may use the same key
    with pytest.raises(errors.UniqueViolation):
        connection.execute(insert, ("merchant-1",))
    connection.rollback()
```

Two merchants are inserted; the same key text can be stored for each, but a second row for the same merchant and key raises `UniqueViolation`.

```python
@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        ("UPDATE bank_accounts SET currency = 'usd' WHERE account_id = 'acct-ada'", "bank_accounts_currency_format"),
        (
            "INSERT INTO customers (customer_id, username, account_id, pin_hash) "
            "VALUES (gen_random_uuid(), 'Ada!', 'acct-ada', 'h')",
            "customers_username_format",
        ),
        (
            "INSERT INTO oauth_clients (client_id, name, secret_hash, public_jwk) VALUES ('Bad Id', 'x', 'h', '{}')",
            "oauth_clients_id_format",
        ),
    ],
)
```

Three malformed values and the constraint that must refuse each: a lower-case currency, a username with a capital letter and `!`, and a merchant id with a space and capitals. `gen_random_uuid()` makes ids inside PostgreSQL.

```python
def test_malformed_identifiers_and_currencies_are_refused(connection, sql, constraint):
    with pytest.raises(errors.CheckViolation, match=constraint):
        connection.execute(sql)
```

Each statement raises `CheckViolation` whose message matches the constraint name.

```python
def test_consents_must_be_consistent(connection):
    connection.execute(
        "INSERT INTO customers (customer_id, username, account_id, pin_hash) VALUES "
        "('11111111-0000-4000-8000-000000000001', 'ada', 'acct-ada', 'h')"
    )
    connection.execute(
        "INSERT INTO oauth_clients (client_id, name, secret_hash, public_jwk) VALUES ('merchant-1', 'm', 'h', '{}')"
    )
    base = (
        "INSERT INTO consents (consent_id, customer_id, payer_account, payee_account, client_id, currency, "
        "max_amount_minor, scope, period_seconds, created_at, expires_at) VALUES (gen_random_uuid(), "
        "'11111111-0000-4000-8000-000000000001', 'acct-ada', 'acct-shop', 'merchant-1', 'USD', %s, %s, %s, now(), "
        "now() + %s)"
    )
    cases = [
        ((0, "single-payment", None, "10 minutes"), "consents_amount_positive"),
        ((100, "forever", None, "10 minutes"), "consents_scope_valid"),
        ((100, "recurring", None, "10 minutes"), "consents_period_matches_scope"),
        ((100, "single-payment", 86400, "10 minutes"), "consents_period_matches_scope"),
        ((100, "single-payment", None, "-1 minutes"), "consents_expiry_valid"),
    ]
    for params, constraint in cases:
        with pytest.raises(errors.CheckViolation, match=constraint):
            connection.execute(base.replace("now() + %s", "now() + %s::interval"), params)
        connection.rollback()
        connection.execute(
            "INSERT INTO customers (customer_id, username, account_id, pin_hash) VALUES "
            "('11111111-0000-4000-8000-000000000001', 'ada', 'acct-ada', 'h') ON CONFLICT DO NOTHING"
        )
        connection.execute(
            "INSERT INTO oauth_clients (client_id, name, secret_hash, public_jwk) "
            "VALUES ('merchant-1', 'm', 'h', '{}') ON CONFLICT DO NOTHING"
        )
    connection.rollback()
```

Inserts a customer and a merchant, then tries five inconsistent consents (`base` is the INSERT with placeholders for the amount, scope, period and lifetime; `%s::interval` casts a text such as `10 minutes` to a length of time): a zero amount, an unknown scope, a recurring consent without a period, a single-payment consent with a period, and an expiry before creation. Each raises `CheckViolation` naming its constraint. After each failure the transaction is rolled back and the customer and merchant are inserted again (`ON CONFLICT DO NOTHING`).

```python
def test_the_seed_funds_the_demo_accounts_through_balanced_entries_and_holds_no_credentials(connection):
    balances = dict(connection.execute("SELECT account_id, balance_minor FROM bank_accounts").fetchall())
    assert balances == {
        "acct-vault": -70_000,
        "acct-ada": 50_000,
        "acct-bob": 20_000,
        "acct-shop": 0,
        "acct-other-shop": 0,
    }
    assert connection.execute("SELECT COALESCE(sum(amount_minor), 0) FROM postings").fetchone() == (0,)
    for table in ("customers", "oauth_clients", "access_tokens", "authentication_proofs"):
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
```

The five seeded balances are exactly as documented, the ledger sums to zero, and the four credential tables (`customers`, `oauth_clients`, `access_tokens`, `authentication_proofs`) are empty: no credential is ever seeded.

```python
def test_the_queries_file_runs_and_its_invariant_queries_return_nothing(connection):
    script = "\n".join(line for line in (SQL / "003_queries.sql").read_text().splitlines() if not line.startswith("\\"))
    cursor = connection.execute(script)
    results = []
    while True:
        results.append(cursor.fetchall() if cursor.description else None)
        if not cursor.nextset():
            break
    assert len(results) == 13
    assert results[1] == [(0,)]  # the ledger sum
    for index in (
        2,
        3,
        5,
        6,
        7,
    ):  # balance mismatches, unbalanced entries, single-payment overuse, ledger gaps, rule breaks
        assert results[index] == [], index
    connection.rollback()
```

Runs `003_queries.sql` after dropping the `psql` meta-command lines (those starting with a backslash). The results are read one after another with `nextset()`. There are 13 results (query 1 has two statements); the ledger sum is 0 and the queries for balance mismatches, unbalanced entries, single-payment overuse, ledger gaps and rule breaks return nothing.

```python
def catalogs(settings: Any) -> tuple[list[Any], list[Any]]:
    catalog_sql = """
        SELECT 'column', c.table_name || '.' || c.column_name,
               c.data_type || ' ' || c.is_nullable || ' ' || COALESCE(c.column_default, '')
          FROM information_schema.columns c WHERE c.table_schema = 'public'
        UNION ALL
        SELECT 'constraint', conrelid::regclass::text || '.' || conname, pg_get_constraintdef(oid) FROM pg_constraint
         WHERE connamespace = 'public'::regnamespace
        UNION ALL
        SELECT 'index', tablename || '.' || indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'
        UNION ALL
        SELECT 'trigger', tgrelid::regclass::text || '.' || tgname,
               regexp_replace(pg_get_triggerdef(oid), ' ON .*? ', ' ON ')
          FROM pg_trigger WHERE NOT tgisinternal
        UNION ALL
        SELECT 'function', proname, prosrc FROM pg_proc WHERE pronamespace = 'public'::regnamespace
        UNION ALL
        SELECT 'comment', c.relname, obj_description(c.oid) FROM pg_class c
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
        ORDER BY 1, 2
    """
    from open_banking.infrastructure.postgres import conninfo

    found = []
    suffix = uuid.uuid4().hex[:8]
    with psycopg.connect(conninfo(settings.postgres), autocommit=True) as admin:
        for label, scripts in (
            ("direct", [SQL / "001_schema.sql"]),
            ("migrated", sorted((SQL / "migrations").glob("*.sql"))),
        ):
            name = f"scratch_{label}_{suffix}"
            admin.execute(f'CREATE DATABASE "{name}"')
            try:
                info = conninfo(settings.postgres).replace(f"dbname={settings.postgres.database}", f"dbname={name}")
                with psycopg.connect(info, autocommit=True) as scratch:
                    for script in scripts:
                        scratch.execute(script.read_text())
                    found.append(scratch.execute(catalog_sql).fetchall())
            finally:
                admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    return found[0], found[1]
```

Builds two scratch databases, one from `001_schema.sql` directly and one by running every migration in order, and returns the contents of the PostgreSQL system catalogs for each. `catalog_sql` is one query joined with `UNION ALL` that lists columns, constraints, indexes, triggers, functions and table comments, sorted. For each label the helper creates a throwaway database with a random suffix, connects to it by replacing the database name in the connection string, runs the scripts and reads the catalog; `finally` drops it with `WITH (FORCE)` even if something failed.

```python
def test_the_migrations_build_the_same_schema_as_001_schema(settings):
    direct, migrated = catalogs(settings)
    assert len(direct) > 80
    assert migrated == direct
```

The two catalogs are identical, and the direct one has more than 80 entries (so the comparison is not vacuous).

### `tests/e2e/conftest.py` (159 lines)

```python
"""Fixtures for the end-to-end tests: the bank and the merchant as two real server processes, and the real programs."""

from __future__ import annotations

import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest
```

Fixtures for the end-to-end tests: the bank and the merchant as two real server processes, and the real programs. New imports: `signal`, `socket`, `subprocess`, `sys`, `time`, `field` and `httpx`.

```python
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
```

Finds an unused port: bind to port 0 (the operating system picks a free one), read it back with `getsockname()` and close.

```python
def run_program(module: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run one of the project's programs the way a user would, in its own process."""
    return subprocess.run(
        [sys.executable, "-m", f"open_banking.interfaces.{module}", *args],
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
```

Runs one of the project's programs the way a user would, in its own process: `python -m open_banking.interfaces.<module> <args>` with the current environment plus extra variables (`*args` collects positional arguments; `{**os.environ, **(env or {})}` merges dictionaries). Output is captured as text, the timeout is 15 minutes and `check=False` means a failing program does not raise by itself.

```python
@dataclass
class Server:
    name: str
    process: subprocess.Popen[str]
    port: int
    output: str = field(default="")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
```

A running server: a name, the process handle, the port and (after it stops) everything it printed. The property `url` builds the address.

```python
def start(name: str, module: str, port: int, env: dict[str, str]) -> Server:
    process = subprocess.Popen(
        [sys.executable, "-m", f"open_banking.interfaces.{module}"],
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the {name} exited early:\n{process.stdout.read() if process.stdout else ''}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                return Server(name, process, port)
        except httpx.HTTPError:
            time.sleep(0.2)
    process.kill()
    raise RuntimeError(f"the {name} did not become healthy in 90 seconds")
```

Starts a server module with the given environment, output captured and errors merged in (`stderr=STDOUT`). It waits up to 90 seconds (`time.monotonic()` never jumps) for `/health` to return 200; if the process exits early the error includes its output; if the time passes the process is killed.

```python
def stop(server: Server) -> tuple[int, str]:
    """SIGTERM, then the exit code and everything the process printed."""
    if server.process.poll() is None:
        server.process.send_signal(signal.SIGTERM)
    try:
        output, _ = server.process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        server.process.kill()
        output, _ = server.process.communicate()
    server.output = output or server.output
    return server.process.returncode, server.output
```

Sends `SIGTERM` if the process is still running, waits up to 60 seconds for it to end and reads everything it printed; if it hangs it is killed. Returns the exit code and the output.

```python
@dataclass
class Stack:
    directory: pathlib.Path
    bank: Server
    merchant: Server
    pin: str
    client_secret: str

    @property
    def outbox(self) -> pathlib.Path:
        return self.directory / "outbox.log"
```

The running scenario: the temporary folder, the two servers, the customer's PIN and the merchant's client secret (so tests can check that neither appears in a log). The property `outbox` is the stand-in phone file.

```python
@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory, database: None) -> Iterator[Stack]:
    directory = tmp_path_factory.mktemp("openbanking")
    admin_env = {
        "BANK_SIGNING_KEY_FILE": str(directory / "bank-key.pem"),
        "OTP_OUTBOX_FILE": str(directory / "outbox.log"),
    }
    registered = run_program(
        "admin_cli",
        "register-merchant",
        "--client-id",
        "merchant-1",
        "--name",
        "Shop",
        "--out",
        str(directory),
        env=admin_env,
    )
    assert registered.returncode == 0, registered.stdout + registered.stderr
    enrolled = run_program(
        "admin_cli",
        "enroll-customer",
        "--username",
        "ada",
        "--account",
        "acct-ada",
        "--out",
        str(directory),
        env=admin_env,
    )
    assert enrolled.returncode == 0, enrolled.stdout + enrolled.stderr
    merchant_env = dict(line.split("=", 1) for line in (directory / "merchant-1.env").read_text().splitlines())
    bank_port, merchant_port = free_port(), free_port()
    bank = start(
        "bank",
        "serve_bank",
        bank_port,
        {
            **admin_env,
            "BANK_HOST": "127.0.0.1",
            "BANK_PORT": str(bank_port),
            "BANK_PUBLIC_URL": f"http://127.0.0.1:{bank_port}",
            "LOG_LEVEL": "INFO",
        },
    )
    merchant = start(
        "merchant",
        "serve_merchant",
        merchant_port,
        {
            **merchant_env,
            "MERCHANT_HOST": "127.0.0.1",
            "MERCHANT_PORT": str(merchant_port),
            "BANK_URL": f"http://127.0.0.1:{bank_port}",
            "MERCHANT_PAYEE_ACCOUNT": "acct-shop",
            "LOG_LEVEL": "INFO",
        },
    )
    running = Stack(
        directory, bank, merchant, (directory / "ada.pin").read_text().strip(), merchant_env["MERCHANT_CLIENT_SECRET"]
    )
    yield running
    for server in (merchant, bank):
        stop(server)
```

A module-scoped fixture (`@pytest.fixture(scope="module")` above it): it builds the whole world **once** per test file. It makes a temporary folder; sets the bank's key and outbox paths for the admin program; runs the admin program to register `merchant-1` and to enroll `ada` (each must exit 0); reads the merchant's environment file the admin step wrote; picks two free ports; starts the bank and then the merchant as real server processes with the right environment (the bank's public URL, the merchant's credentials, the bank's URL, the payee account); and yields a `Stack` with the PIN and the secret read from the files. After the tests it stops the merchant and then the bank.

### `tests/e2e/test_lesson_scenario.py` (168 lines)

```python
"""The lesson's scenario through real processes: a bank, a merchant, and the customer's program, failures included."""

from __future__ import annotations

import json
from typing import Any

import httpx
import psycopg
import pytest

from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.postgres import conninfo
from tests.conftest import load_schema, wait_until_ready
from tests.e2e.conftest import Stack, run_program, stop
```

The lesson's scenario through real processes: a bank, a merchant, and the customer's program, failures included. `psycopg` is used to reload the schema before the demo test.

```python
pytestmark = pytest.mark.e2e
```

Marks every test in the file as `e2e`.

```python
def approve(stack: Stack, *extra: str):
    return run_program(
        "customer_cli",
        "--bank-url",
        stack.bank.url,
        "approve",
        "--username",
        "ada",
        "--account",
        "acct-ada",
        "--payee",
        "acct-shop",
        "--merchant",
        "merchant-1",
        "--max",
        "50.00",
        "--pin-file",
        str(stack.directory / "ada.pin"),
        "--outbox",
        str(stack.outbox),
        *extra,
    )
```

A helper that runs the **customer program** `approve` command against the bank: user `ada`, her account, the shop as payee, `merchant-1`, a 50.00 maximum, the PIN from her file and the code from the stand-in phone file (`--outbox`); extra arguments are appended (`*extra`).

```python
def consent_id_of(completed) -> str:
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return str(json.loads(completed.stdout.strip().splitlines()[-1])["consent_id"])
```

Requires the program to have exited 0 and reads the consent id from the last line of its output (a JSON line).

```python
def checkout(stack: Stack, amount_minor: int = 3000) -> dict[str, Any]:
    response = httpx.post(
        f"{stack.merchant.url}/v1/checkouts", json={"amount_minor": amount_minor, "currency": "USD"}, timeout=30
    )
    assert response.status_code == 201
    return dict(response.json())
```

Creates a checkout at the merchant over HTTP (201) and returns its JSON.

```python
def complete(stack: Stack, checkout_id: str, consent_id: str) -> httpx.Response:
    return httpx.post(
        f"{stack.merchant.url}/v1/checkouts/{checkout_id}/complete", json={"consent_id": consent_id}, timeout=60
    )
```

Completes a checkout at the merchant with a consent id.

```python
def balances(stack: Stack) -> dict[str, str]:
    completed = run_program(
        "admin_cli",
        "balances",
        "acct-ada",
        "acct-shop",
        env={"OTP_OUTBOX_FILE": str(stack.outbox), "BANK_SIGNING_KEY_FILE": str(stack.directory / "bank-key.pem")},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return dict(line.split(": ", 1) for line in completed.stdout.strip().splitlines())
```

Runs the admin program's `balances` command for Ada and the shop and turns its lines (`account: 500.00 USD`) into a dictionary by splitting each line at `: `.

```python
def test_the_customer_approves_at_the_bank_and_the_merchant_is_paid_once(stack):
    before = balances(stack)
    created = checkout(stack)
    assert created["consent_request"]["client_id"] == "merchant-1" and created["status"] == "AWAITING_CONSENT"
    consent = consent_id_of(approve(stack))  # the customer and the bank only; the merchant is not involved
    done = complete(stack, created["checkout_id"], consent)
    assert done.status_code == 200 and done.json()["status"] == "PAID"
    again = complete(stack, created["checkout_id"], consent)
    assert again.status_code == 200 and again.json()["payment_id"] == done.json()["payment_id"]
    after = balances(stack)
    assert (
        before["acct-ada"] == "500.00 USD" and after["acct-ada"] == "470.00 USD" and after["acct-shop"] == "30.00 USD"
    )
    assert after["ledger sum across every posting"] == "0.00"
```

The whole happy path: the merchant's checkout names the terms; the customer approves at the bank only (the merchant is not involved); the merchant completes the checkout (200, `PAID`); completing again returns the same payment id; Ada goes from 500.00 to 470.00, the shop has 30.00 and the ledger sums to `0.00`.

```python
def test_an_abandoned_authentication_creates_no_consent_and_nothing_to_pay_with(stack):
    abandoned = approve(stack, "--abandon")
    assert abandoned.returncode == 0 and "no consent created" in abandoned.stdout
    created = checkout(stack)
    refused = complete(stack, created["checkout_id"], "00000000-0000-4000-8000-0000000000ff")
    assert refused.status_code == 403 and refused.json()["refusal"]["code"] == "consent_not_found"
    assert balances(stack)["acct-ada"] == "470.00 USD"
```

`--abandon` stops after the code is sent: the program exits 0 and says no consent was created. Completing a checkout with a made-up consent id is a 403 refusal `consent_not_found`; Ada's balance is unchanged.

```python
def test_a_wrong_pin_is_refused_by_the_bank(stack, tmp_path):
    wrong = tmp_path / "wrong.pin"
    wrong.write_text("000000\n")
    completed = run_program(
        "customer_cli", "--bank-url", stack.bank.url, "approve", "--username", "ada", "--account", "acct-ada",
        "--payee", "acct-shop", "--merchant", "merchant-1", "--max", "50.00", "--pin-file", str(wrong),
        "--outbox", str(stack.outbox),
    )  # fmt: skip
    assert completed.returncode == 1 and "authentication_failed" in completed.stdout
```

A PIN file with `000000`: the customer program exits 1 and prints `authentication_failed`. (`# fmt: skip` after the call keeps the formatter from reflowing the long argument list.)

```python
def test_a_consent_the_customer_revoked_moves_no_money(stack):
    consent = consent_id_of(approve(stack))
    revoked = run_program(
        "customer_cli", "--bank-url", stack.bank.url, "revoke", "--username", "ada", "--consent-id", consent,
        "--pin-file", str(stack.directory / "ada.pin"),
    )  # fmt: skip
    assert revoked.returncode == 0 and "revoked" in revoked.stdout
    refused = complete(stack, checkout(stack)["checkout_id"], consent)
    assert refused.status_code == 403 and refused.json()["refusal"]["code"] == "consent_revoked"
    assert balances(stack)["acct-ada"] == "470.00 USD"
```

The customer approves, then revokes with the `revoke` command (exit 0, output contains `revoked`); the merchant's attempt with that consent is refused as `consent_revoked`; Ada's balance is unchanged.

```python
def test_the_servers_report_healthy_and_publish_the_banks_key(stack):
    assert httpx.get(f"{stack.bank.url}/health", timeout=10).json() == {"status": "ok", "checks": {"database": "ok"}}
    assert httpx.get(f"{stack.merchant.url}/health", timeout=10).json() == {"status": "ok"}
    assert len(httpx.get(f"{stack.bank.url}/.well-known/bank-keys.json", timeout=10).json()["keys"]) == 1
```

The bank's `/health` reports the database ok; the merchant's reports ok; the bank publishes exactly one key.

```python
def test_both_servers_stop_gracefully_and_their_logs_hold_no_secrets(stack):
    codes = [json.loads(line)["code"] for line in stack.outbox.read_text().splitlines()]
    results = {server.name: stop(server) for server in (stack.merchant, stack.bank)}
    for name, (code, output) in results.items():
        assert code == 0, (name, output)
        assert "shutdown complete" in output and "level=ERROR" not in output
        for secret in (stack.pin, stack.client_secret, *codes):
            assert secret not in output, (name, "a secret reached the log")
        assert "payment posted" in output or name == "merchant"
    assert "checkout paid" in results["merchant"][1]
```

Reads the codes that were sent to the phone, then stops both servers with SIGTERM. Each must exit 0, log `shutdown complete` and no `level=ERROR`, and its output must contain **none** of the PIN, the client secret or any code. The bank's log shows `payment posted`; the merchant's shows `checkout paid`.

```python
def test_the_demo_program_runs_the_whole_scenario_and_exits_zero(settings: BankConfig):
    # The demo enrolls its own customers, and the `stack` fixture of this module enrolled "ada" already, so the demo
    # starts from a freshly loaded schema (what `make run` does with `make db`).
    wait_until_ready(settings)
    with psycopg.connect(conninfo(settings.postgres), autocommit=True) as admin:
        load_schema(admin)
    completed = run_program("demo_cli")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    for line in (
        "consent without authentication -> 401",
        "abandoned authentication: the code was sent",
        "three wrong codes -> wrong_code, wrong_code, challenge_locked",
        "a proof for 50.00 used for a consent of 5,000.00 -> 401",
        "a stolen token used with another key -> 401",
        "another merchant presents this consent -> consent_not_granted_to_caller",
        "amount=60.00                    -> 403 amount_exceeds_consent: amount 60.00 exceeds consent max 50.00",
        "payee_account=acct-other-shop   -> 403 payee_not_covered: consent does not cover this payee",
        "amount=30.00, correct payee     -> Success",
        "clock 1 second past expires_at  -> 403 consent_expired: consent expired",
        "Idempotent-Replay=true",
        "amount=35.00, signature covers amount=30.00 -> 400 request_not_as_signed",
        "revoked consent: revoke -> 200; initiate -> 403 consent_revoked: consent revoked",
        "one payment, not two",
        "period_cap_exceeded",
        "ledger sum across every posting: 0.00",
    ):
        assert line in completed.stdout, line
    assert "FAILED" not in completed.stdout
```

The demo enrolls its own customers, and the module's `stack` fixture already enrolled `ada`, so the test first reloads the schema (what `make run` does with `make db`). It then runs the demo program, requires exit 0, and requires that its output contains sixteen expected lines, one for each rule of the lesson (the consent without authentication, the abandoned authentication, the locked challenge, the proof tied to terms, the stolen token, another merchant's consent, each refusal, the successful payment, expiry, replay, tampering, revocation, the lost response, the period cap and the zero ledger sum), and no `FAILED`.

## Try it yourself

1. Start the database, then run the layers (each is a separate make target):

   ```bash
   make up
   make test-integration
   make test-e2e
   make down
   ```

   Expected: all integration tests pass, then all end-to-end tests pass.

2. Watch a constraint refuse bad data: run `make db`, open `psql` and try `UPDATE bank_accounts SET balance_minor = balance_minor + 100 WHERE account_id = 'acct-shop'` followed by `COMMIT`; compare the error with `test_a_balance_that_moved_without_a_posting_cannot_commit`.

## Mistakes people make

- **Tests that leave data behind.** Every test reloads the schema; the constraint tests roll back.
- **Races that only sometimes show.** Starting threads on a barrier gives them the same instant, and some tests repeat.
- **Fixed ports.** The e2e fixture asks the operating system for free ports.
- **Waiting without a limit.** Every wait here has a timeout.

## Check yourself

1. What does a `Barrier(20)` do? *Makes 20 threads wait until all 20 have arrived, then releases them together.*
2. Why does the opposite-payments test not deadlock? *Both payments lock the two accounts in the same sorted order.*
3. How does the e2e fixture know a server is ready? *It polls `/health` until it returns 200.*
4. Why must the demo test reload the schema? *The demo enrolls its own customers and the module's fixture already enrolled one.*
5. What exit code must a server give after SIGTERM? *0, after logging `shutdown complete`.*

## Further reading

- pytest documentation: https://docs.pytest.org/en/stable/
- pytest fixtures: https://docs.pytest.org/en/stable/how-to/fixtures.html
- pytest parametrize: https://docs.pytest.org/en/stable/how-to/parametrize.html
- Hypothesis documentation: https://hypothesis.readthedocs.io/
- Python 3.12 `threading`: https://docs.python.org/3.12/library/threading.html
- psycopg 3 documentation: https://www.psycopg.org/psycopg3/docs/
