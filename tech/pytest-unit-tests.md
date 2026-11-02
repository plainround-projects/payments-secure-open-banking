# pytest walkthrough: the unit tests, line by line

## What it is

This note reads every line of the tests that need no database: the shared fixtures, the in-memory fakes, the helper flows, and the tests of the domain, the use cases, the cryptography, the settings and the two HTTP APIs. For the general idea of pytest see `pytest.md`; for the property test see `hypothesis.md`.

A **unit test** checks one small piece in isolation. Here the "isolation" is a set of **fakes**: simple Python objects that behave like the database, the clock and the phone but keep everything in memory. The cryptography is **not** faked: hashing, signing and proofs of possession are the real code, so the security checks are really exercised, and each test still runs in milliseconds.

## Why this repository uses it

The lesson's promises (no consent without two factors for exactly the approved terms, a token only works with its key, a payment only within consent, money moves once however often a request is repeated) must be checked after every change. Unit tests check them in seconds without Docker, so the first command a developer runs (`make check`) can include them, together with the 95 percent coverage bar on the domain and application.

## Where it appears

| File | What it is |
| --- | --- |
| `tests/conftest.py` | Root fixtures: the settings, the wait for the database and the schema loader. |
| `tests/unit/conftest.py` | Unit fixtures: the bank on in-memory storage with real cryptography. |
| `tests/unit/helpers.py` | Shared flows: authenticate, grant, sign, pay, prove possession. |
| `tests/unit/fakes.py` | In-memory stand-ins for the database, clock and ids. |
| `tests/unit/domain/test_money.py` | Tests of Money. |
| `tests/unit/domain/test_ledger_and_signing.py` | Tests of transfers, the signed request and keys. |
| `tests/unit/domain/test_consent_and_authentication.py` | Tests of consent rules and the challenge. |
| `tests/unit/application/test_access.py` | Merchant tokens and proofs of possession. |
| `tests/unit/application/test_authentication_and_consent.py` | Strong authentication and consent. |
| `tests/unit/application/test_merchant_checkout.py` | The merchant's checkout. |
| `tests/unit/application/test_payments.py` | Payments, replays, refusals, races and a property test. |
| `tests/unit/infrastructure/test_crypto_and_keys.py` | Hashes, digests, signatures, thumbprints, DPoP. |
| `tests/unit/infrastructure/test_settings_otp_faults.py` | Settings, the code channels and scripted faults. |
| `tests/unit/infrastructure/test_logging_retry.py` | Log redaction and the retry helper. |
| `tests/unit/interfaces/test_bank_api.py` | The bank's HTTP API and the merchant's client. |
| `tests/unit/interfaces/test_merchant_api_and_clis.py` | The merchant's API and the programs' pure parts. |

## How it works underneath

pytest imports each `test_*.py` file and every `conftest.py` above it. A test function's parameters are matched by name with **fixtures**; pytest calls the fixture and passes the result in. The functions use plain `assert`; when an assertion is false pytest shows the values on both sides. The in-memory `unit_of_work` copies the state before a block and puts it back if the block raises, which is what a database rollback does. The one-time codes come from a known sequence so a test can complete an authentication. The property test lets Hypothesis generate many random sequences of operations and checks that invariants hold after each one.

## Walkthrough of this repository's use

Files are shown in the order they are used: fixtures, helpers and fakes first, then the tests, from the innermost layer to the outermost.

### `tests/conftest.py` (49 lines)

```python
"""Fixtures for the tests that need the PostgreSQL container from docker-compose.yml (`make up`)."""

from __future__ import annotations

import pathlib
import time
from typing import Any

import psycopg
import pytest

from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.postgres import conninfo

SQL = pathlib.Path(__file__).resolve().parent.parent / "sql"
```

Fixtures for the tests that need the PostgreSQL container from `docker-compose.yml` (`make up`). `time` is used for waiting; `pathlib.Path` represents file paths; `BankConfig` is the bank's configuration class; `SQL` is the folder `sql/` found relative to this file (`Path(__file__).resolve().parent.parent / "sql"`).

```python
@pytest.fixture(scope="session")
def settings() -> BankConfig:
    """From the environment (make loads .env; CI sets the variables); the password has no default."""
    return BankConfig.from_env()
```

`settings` reads the bank's configuration from the environment once per test run (`scope="session"`). The docstring notes that make loads `.env` (which has the generated pepper) and CI sets the variables, and that the password has no default.

```python
def wait_until_ready(settings: Any, seconds: float = 180.0) -> None:
    """Wait for the database to accept connections: it may be starting, or recovering after a starved container."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            with psycopg.connect(conninfo(settings.postgres), connect_timeout=5) as connection:
                connection.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1.0)
```

Waits up to 180 seconds for the database to accept connections: it may be starting, or recovering after the container was starved of resources. It loops with `while True`: try to connect and run `SELECT 1`, and `return` on success; on `psycopg.OperationalError` (not ready) it raises the error again if the deadline has passed (`time.monotonic()` is a clock that never jumps), otherwise sleeps one second and tries again.

```python
def load_schema(connection: psycopg.Connection[Any]) -> None:
    """Drop everything and apply 001_schema.sql and 002_seed.sql: the same as `make db`."""
    for script in ("reset.sql", "001_schema.sql", "002_seed.sql"):
        connection.execute((SQL / script).read_text())
```

Runs the three SQL files in order (`reset.sql`, `001_schema.sql`, `002_seed.sql`) by reading each file's text and executing it: the same as `make db`.

```python
@pytest.fixture(scope="session")
def database(settings: BankConfig) -> None:
    """A fresh schema with the seed, once per test session. This drops whatever the database holds."""
    wait_until_ready(settings)
    with psycopg.connect(conninfo(settings.postgres), autocommit=True) as connection:
        load_schema(connection)
```

A session-scoped fixture (the `@pytest.fixture(scope="session")` line above it): it first waits until the database is ready, then connects with `autocommit=True` (each statement applies at once) and loads the schema and seed once per run. The docstring warns that it drops whatever the database holds.

### `tests/unit/conftest.py` (95 lines)

```python
"""Fixtures: the bank wired to in-memory storage with real cryptography, a fixed clock and a known one-time code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from open_banking.application.bank.ports import BankSettings
from open_banking.infrastructure import keys
from open_banking.infrastructure.crypto import StandardCrypto
from open_banking.infrastructure.otp import MemoryOtpChannel
from open_banking.interfaces.bank_container import BankContainer, assemble_bank
from tests.unit.fakes import FixedClock, InMemoryDatabase, SequentialIds
```

Shared test setup (a **fixture** file: pytest reads `conftest.py` automatically). The docstring: the bank wired to in-memory storage with **real** cryptography, a fixed clock and a known one-time code. Only the storage, the clock and the code source are replaced; the hashing and signing are the real code, so the security checks are really exercised.

```python
BANK_URL = "http://bank.test"
```

The public URL the test bank pretends to have. Proofs of possession are bound to it.

```python
def default_settings() -> BankSettings:
    return BankSettings(
        challenge_time_to_live=timedelta(minutes=5),
        proof_time_to_live=timedelta(minutes=2),
        token_time_to_live=timedelta(minutes=10),
        consent_time_to_live=timedelta(minutes=10),
        recurring_consent_time_to_live=timedelta(days=90),
        dpop_max_skew=timedelta(seconds=60),
        idempotency_key_time_to_live=timedelta(hours=24),
    )
```

The bank's timing rules for tests: challenge 5 minutes, proof 2 minutes, token 10 minutes, consent 10 minutes, recurring consent 90 days, DPoP skew 60 seconds, idempotency keys 24 hours. They match the real defaults.

```python
@dataclass
class Merchant:
    """A registered merchant as its own process would hold it: id, secret, key pair."""

    client_id: str
    secret: str
    private_pem: str
    public_jwk: dict[str, Any]
```

`@dataclass` writes the constructor. A registered merchant as its own process would hold it: client id, secret, private key (PEM) and public key (JWK).

```python
@dataclass
class World:
    """Everything a test needs: the bank, its storage, the clock, the phone, the people."""

    bank: BankContainer
    db: InMemoryDatabase
    clock: FixedClock
    phone: MemoryOtpChannel
    crypto: StandardCrypto
    merchant: Merchant
    other_merchant: Merchant
    ada_pin: str = "246810"
    bob_pin: str = "135791"
```

Everything a test needs: the bank container, its in-memory storage, the clock, the stand-in phone, the crypto object, two merchants, and the PINs of the two customers (fields with defaults come last: `ada_pin: str = "246810"`).

```python
def build_world() -> World:
    db = InMemoryDatabase()
    db.open_account("acct-vault", 0, vault=True)
    db.open_account("acct-ada", 50_000)
    db.open_account("acct-bob", 20_000)
    db.open_account("acct-shop", 0)
    db.open_account("acct-other-shop", 0)
    clock = FixedClock()
    phone = MemoryOtpChannel()
    codes = iter(f"{n:06d}" for n in range(111_111, 999_999, 111_111))
    crypto = StandardCrypto(pepper="test-pepper-0123456789", scrypt_n=16, code_source=lambda: next(codes))
    bank = assemble_bank(
        uow_factory=db.unit_of_work,
        crypto=crypto,
        otp=phone,
        clock=clock,
        ids=SequentialIds(),
        settings=default_settings(),
        public_url=BANK_URL,
        signing_pem=keys.generate_keypair()[0],
        probes={},
    )
    world = World(bank, db, clock, phone, crypto, _register(bank, "merchant-1"), _register(bank, "merchant-2"))
    bank.enroll_customer.execute("ada", world.ada_pin, "acct-ada")
    bank.enroll_customer.execute("bob", world.bob_pin, "acct-bob")
    return world
```

Builds the world. The in-memory database gets five accounts (the vault, Ada with 500.00, Bob with 200.00, the shop and the other shop). `codes = iter(f"{n:06d}" for n in range(111_111, 999_999, 111_111))` makes an iterator over the six-digit codes `111111`, `222222`, ... so each challenge gets a known code (`iter` makes something `next()` can step through). `StandardCrypto` is real, with a test pepper, a **tiny** scrypt cost (`scrypt_n=16`, so hashing is fast in tests) and that code source (`next(codes)`). `assemble_bank` is the real composition root with the in-memory unit of work, the stand-in phone, the fixed clock, sequential ids, the default settings, a freshly generated signing key and no health probes. Then two merchants are registered and the customers `ada` and `bob` are enrolled with their PINs.

```python
def _register(bank: BankContainer, client_id: str) -> Merchant:
    pem, jwk = keys.generate_keypair()
    secret = f"secret-of-{client_id}-0123456789"
    bank.register_client.execute(client_id, client_id, secret, jwk)
    return Merchant(client_id, secret, pem, jwk)
```

Registers one merchant through the real use case: it generates a key pair and a secret, registers them (the bank gets the secret as a hash and only the public key), and returns the merchant's own view with the private key.

```python
@pytest.fixture
def world() -> World:
    return build_world()
```

`@pytest.fixture` marks `world()` as a **fixture**: a test with a parameter named `world` receives a fresh `World`.

### `tests/unit/helpers.py` (96 lines)

```python
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
```

Small flows shared by the tests: authenticate and grant a consent, sign and make a payment. They call the **use cases** directly (no HTTP), so many tests can build on them.

```python
USD = "USD"
```

A shorthand for the currency.

```python
def terms(
    payer: str = "acct-ada",
    payee: str = "acct-shop",
    client: str = "merchant-1",
    maximum: int = 5000,
    scope: ConsentScope = ConsentScope.SINGLE_PAYMENT,
    period: timedelta | None = None,
) -> ConsentTerms:
    return ConsentTerms(payer, payee, client, Money(maximum, USD), scope, period)
```

Builds consent terms with defaults (Ada pays the shop through `merchant-1`, up to 50.00, single payment); any default can be overridden by name.

```python
def authenticate(world: Any, username: str, pin: str, consent_terms: ConsentTerms) -> str:
    """PIN, then the code from the phone. Returns the proof's bearer value."""
    started = world.bank.start_challenge.execute(username, pin, consent_terms)
    customer = world.db.customers_by_name(username)
    code = world.phone.latest_code(customer.customer_id)
    assert code is not None
    verification = world.bank.verify_challenge.execute(started.challenge_id, code)
    assert verification.proof is not None
    return str(verification.proof)
```

PIN, then the code from the phone: starts the challenge, looks up the customer, reads the newest code the phone received for them (`assert code is not None` fails the test if none arrived), verifies it and returns the proof's bearer value.

```python
def grant(world: Any, consent_terms: ConsentTerms | None = None, username: str = "ada") -> Consent:
    """A consent granted the proper way."""
    consent_terms = consent_terms or terms()
    pin = world.ada_pin if username == "ada" else world.bob_pin
    granted: Consent = world.bank.grant_consent.execute(
        authenticate(world, username, pin, consent_terms), consent_terms
    )
    return granted
```

A consent granted the proper way: authenticate with the right PIN for the user, then grant the consent with that proof.

```python
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
```

Signs a payment request as a merchant: `keys.sign` over the canonical bytes, built from the merchant id (or another one, to test mismatches), consent id, payee, amount and key.

```python
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
```

A payment request signed by a merchant (`merchant-1` by default), sent straight to the use case. `signed_minor` lets a test sign a different amount than it sends (`signed_minor or amount_minor` uses the first when it is not zero or `None`). It returns the stored response and whether it was a replay.

```python
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
```

A proof of possession from a merchant's key for one request, bound to the test bank's URL plus the path, to the method, to the current test time and optionally to an access token.

### `tests/unit/fakes.py` (307 lines)

```python
"""In-memory adapters for the bank's ports, so the use cases are tested without a database.

`InMemoryDatabase.unit_of_work` behaves like a transaction: it holds a lock for the block (so concurrent callers queue
the way they do on the advisory lock and the row locks) and restores a snapshot if the block raises.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from open_banking.application.bank.ports import (
    AccessToken,
    Account,
    Claim,
    Customer,
    Fresh,
    OAuthClient,
    PaymentRecord,
    Replay,
    Reused,
    UnitOfWork,
)
from open_banking.domain.authentication import Challenge
from open_banking.domain.consent import AuthenticationProof, Consent
from open_banking.domain.idempotency import IdempotencyKey, StoredResponse
from open_banking.domain.ledger import Transfer
from open_banking.domain.money import Money

START = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
```

**Fakes** are simple stand-ins for real parts. The docstring explains the central one: `InMemoryDatabase.unit_of_work` behaves like a transaction. It holds a lock for the whole block (so concurrent callers queue the way they do on the advisory lock and the row locks) and restores a snapshot if the block raises. `START` is a fixed moment used as "now" so no test depends on today's date.

```python
class FixedClock:
    """A clock that moves only when told to."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta
```

`FixedClock.now()` returns the same time until a test calls `advance(delta)`, which moves it forward with `+=` on a `timedelta`. That makes expiry tests exact.

```python
class SequentialIds:
    """Predictable UUIDs: 00000000-0000-4000-8000-000000000001, ...0002, ..."""

    def __init__(self) -> None:
        self._counter = 0
        self._lock = threading.Lock()

    def new_id(self) -> UUID:
        with self._lock:
            self._counter += 1
            return UUID(f"00000000-0000-4000-8000-{self._counter:012d}")
```

Predictable ids: a counter increases on each call (under a lock, because several threads may ask) and the UUID is built from it with `f"...{self._counter:012d}"` (`:012d` pads the number with zeros to 12 digits), giving `00000000-0000-4000-8000-000000000001`, then `...0002`, then `...0003`, one more each time.

```python
class InMemoryDatabase:
    """The state of all repositories, with snapshot-based rollback."""

    def __init__(self) -> None:
        self.accounts: dict[str, Account] = {}
        self.postings: list[tuple[str, str, str, int]] = []  # (kind, reference, account, amount)
        self.customers: dict[str, Customer] = {}
        self.clients: dict[str, OAuthClient] = {}
        self.challenges: dict[str, Challenge] = {}
        self.proofs: dict[str, AuthenticationProof] = {}
        self.consents: dict[str, Consent] = {}
        self.tokens: dict[str, AccessToken] = {}
        self.seen_proofs: set[str] = set()
        self.payments: dict[str, PaymentRecord] = {}
        self.keys: dict[tuple[str, str], tuple[str, StoredResponse, datetime]] = {}
        self._lock = threading.RLock()
```

The state of all repositories as plain Python containers: dictionaries of accounts, customers, clients, challenges, proofs, consents, tokens, payments and idempotency keys, a list of postings, a set of seen proof ids, and a re-entrant lock. The comment on the postings list shows the tuple layout.

```python
    def open_account(self, account_id: str, minor: int, *, vault: bool = False) -> None:
        self.accounts[account_id] = Account(account_id, account_id, Money(minor, "USD"), vault)
        if minor:
            self.postings.append(("FUNDING", account_id, account_id, minor))
            self.postings.append(("FUNDING", account_id, "acct-vault", -minor))
```

Opens an account with a balance and, if the balance is not zero, two funding postings (the account's credit and the vault's debit) so the fake ledger sums to zero from the start.

```python
    def _state(self) -> tuple[object, ...]:
        return (
            dict(self.accounts),
            list(self.postings),
            dict(self.customers),
            dict(self.clients),
            dict(self.challenges),
            dict(self.proofs),
            dict(self.consents),
            dict(self.tokens),
            set(self.seen_proofs),
            dict(self.payments),
            dict(self.keys),
        )
```

A snapshot of everything: copies of the dictionaries, the list and the set, in a tuple.

```python
    def _restore(self, state: tuple[object, ...]) -> None:
        (
            self.accounts,
            self.postings,
            self.customers,
            self.clients,
            self.challenges,
            self.proofs,
            self.consents,
            self.tokens,
            self.seen_proofs,
            self.payments,
            self.keys,
        ) = state  # type: ignore[assignment]
```

Puts a snapshot back by assigning all eleven values at once; `# type: ignore[assignment]` tells the type checker not to complain about the generic tuple.

```python
    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        with self._lock:
            snapshot = self._state()
            try:
                yield _InMemoryUnitOfWork(self)
            except BaseException:
                self._restore(snapshot)
                raise
```

`unit_of_work` is a context manager: under the lock it takes a snapshot, yields a unit of work to the test code, and if the block raises *anything* (`except BaseException`) puts the snapshot back and re-raises. That is what a database rollback does.

```python
    def ledger_sum(self) -> int:
        return sum(row[3] for row in self.postings)

    def balance(self, account_id: str) -> int:
        return self.accounts[account_id].balance.minor

    def customers_by_name(self, username: str) -> Customer:
        return next(c for c in self.customers.values() if c.username == username)
```

Test helpers: `ledger_sum` adds up every stored posting; `balance` reads an account's balance in minor units; `customers_by_name` finds a customer (`next(...)` takes the first match of a generator expression).

```python
class _Accounts:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def get(self, account_id: str) -> Account | None:
        return self._db.accounts.get(account_id)

    def get_for_update(self, account_id: str) -> Account | None:
        return self._db.accounts.get(account_id)
```

The in-memory account repository; `get` and `get_for_update` are the same (the whole database is locked).

```python
class _Customers:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def by_username(self, username: str) -> Customer | None:
        return next((c for c in self._db.customers.values() if c.username == username), None)

    def by_id(self, customer_id: str) -> Customer | None:
        return self._db.customers.get(customer_id)

    def add(self, customer: Customer) -> None:
        self._db.customers[customer.customer_id] = customer
```

Customers: find by username (`next(generator, None)` gives the first match or `None`), find by id, add.

```python
class _Clients:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def get(self, client_id: str) -> OAuthClient | None:
        return self._db.clients.get(client_id)

    def add(self, client: OAuthClient) -> None:
        self._db.clients[client.client_id] = client
```

Merchants: get and add.

```python
class _Challenges:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, challenge: Challenge) -> None:
        self._db.challenges[challenge.challenge_id] = challenge

    def get_for_update(self, challenge_id: str) -> Challenge | None:
        return self._db.challenges.get(challenge_id)

    def save(self, challenge: Challenge) -> None:
        self._db.challenges[challenge.challenge_id] = challenge
```

Challenges: add, get and save.

```python
class _Proofs:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, proof: AuthenticationProof) -> None:
        self._db.proofs[proof.proof_id] = proof

    def get_for_update(self, proof_id: str) -> AuthenticationProof | None:
        return self._db.proofs.get(proof_id)

    def mark_consumed(self, proof_id: str) -> None:
        self._db.proofs[proof_id] = replace(self._db.proofs[proof_id], consumed=True)
```

Proofs: add, get, and `mark_consumed`, which stores a copy with `consumed=True` (`dataclasses.replace`).

```python
class _Consents:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, consent: Consent) -> None:
        self._db.consents[consent.consent_id] = consent

    def get_for_update(self, consent_id: str) -> Consent | None:
        return self._db.consents.get(consent_id)

    def save(self, consent: Consent) -> None:
        self._db.consents[consent.consent_id] = consent

    def count(self) -> int:
        return len(self._db.consents)

    def spent_since(self, consent_id: str, since: datetime, currency: str) -> Money:
        total = sum(
            p.amount.minor
            for p in self._db.payments.values()
            if p.consent_id == consent_id and p.created_at >= since and p.amount.currency == currency
        )
        return Money(total, currency)
```

Consents: add, get, save, count, and `spent_since`, which sums the amounts of this consent's payments created at or after a moment in the given currency.

```python
class _Tokens:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, token: AccessToken) -> None:
        self._db.tokens[token.token_digest] = token

    def get(self, token_digest: str) -> AccessToken | None:
        return self._db.tokens.get(token_digest)

    def remember_proof(self, jti: str, at: datetime) -> bool:
        if jti in self._db.seen_proofs:
            return False
        self._db.seen_proofs.add(jti)
        return True
```

Tokens: add, get by digest, and `remember_proof`, which returns `False` if the proof id was already in the set and otherwise adds it and returns `True`.

```python
class _Payments:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, payment: PaymentRecord) -> None:
        self._db.payments[payment.payment_id] = payment

    def get(self, payment_id: str, client_id: str) -> PaymentRecord | None:
        payment = self._db.payments.get(payment_id)
        return payment if payment is not None and payment.client_id == client_id else None
```

Payments: add, and get by id and client (`payment if payment is not None and payment.client_id == client_id else None`).

```python
class _Ledger:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def post(self, transfer: Transfer, kind: str, reference: str, at: datetime) -> None:
        if any(row[0] == kind and row[1] == reference for row in self._db.postings):
            raise RuntimeError(f"{kind} {reference} was already posted")
        for posting in transfer.postings():
            self._db.postings.append((kind, reference, posting.account_id, posting.amount.minor))
            account = self._db.accounts[posting.account_id]
            self._db.accounts[posting.account_id] = replace(account, balance=account.balance + posting.amount)

    def total(self, currency: str) -> Money:
        return Money(self._db.ledger_sum(), currency)
```

The in-memory ledger. `post` refuses a second posting of the same kind and reference (raising `RuntimeError`), then appends one tuple per posting and moves each account's balance (`replace(account, balance=account.balance + posting.amount)`). `total` returns the sum of all postings.

```python
class _Keys:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def claim(self, client_id: str, key: IdempotencyKey, request_fingerprint: str, now: datetime) -> Claim:
        stored = self._db.keys.get((client_id, key.value))
        if stored is None:
            return Fresh()
        fingerprint, response, expires_at = stored
        if expires_at <= now:
            del self._db.keys[(client_id, key.value)]
            return Fresh()
        return Replay(response) if fingerprint == request_fingerprint else Reused()

    def store(
        self,
        client_id: str,
        key: IdempotencyKey,
        request_fingerprint: str,
        response: StoredResponse,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        self._db.keys[(client_id, key.value)] = (request_fingerprint, response, expires_at)
```

The key store with the same behavior as the SQL version: `claim` returns `Fresh()` for an unknown key (and deletes and treats as fresh an expired one), `Replay(response)` for the same fingerprint and `Reused()` otherwise; `store` saves the fingerprint, the response and the expiry.

```python
class _InMemoryUnitOfWork:
    def __init__(self, db: InMemoryDatabase) -> None:
        self.accounts = _Accounts(db)
        self.customers = _Customers(db)
        self.clients = _Clients(db)
        self.challenges = _Challenges(db)
        self.proofs = _Proofs(db)
        self.consents = _Consents(db)
        self.tokens = _Tokens(db)
        self.payments = _Payments(db)
        self.ledger = _Ledger(db)
        self.keys = _Keys(db)
```

Bundles the ten repositories.

### `tests/unit/domain/test_money.py` (65 lines)

```python
import pytest

from open_banking.domain.errors import CurrencyMismatchError, InvalidMoneyError
from open_banking.domain.money import Money, sum_money
```

Tests of `Money`. A test is a function whose name starts with `test_`; it passes if it finishes without raising. `assert` raises `AssertionError` when its condition is false. `pytest.raises(SomeError)` is a block that passes only if the code inside raises that error.

```python
def test_parse_reads_dollars_into_exact_cents():
    assert Money.parse("42.50", "USD") == Money(4250, "USD")
```

Reading `"42.50"` gives exactly 4250 cents.

```python
def test_parse_keeps_a_whole_number_of_yen_as_is():
    assert Money.parse("500", "JPY") == Money(500, "JPY")
```

Yen have no minor units, so `"500"` is 500.

```python
@pytest.mark.parametrize("text", ["42.505", "abc", "NaN", "Infinity", ""])
def test_parse_refuses_what_is_not_an_exact_amount(text):
    with pytest.raises(InvalidMoneyError):
        Money.parse(text, "USD")
```

`@pytest.mark.parametrize` runs one test body once per listed value. These five texts (three decimals, not a number, NaN, infinity, empty) must all raise `InvalidMoneyError`.

```python
def test_parse_refuses_decimals_in_a_currency_without_minor_units():
    with pytest.raises(InvalidMoneyError):
        Money.parse("5.5", "JPY")
```

`"5.5"` yen is refused.

```python
def test_an_unknown_currency_is_refused():
    with pytest.raises(InvalidMoneyError):
        Money(100, "XXX")
    with pytest.raises(InvalidMoneyError):
        Money.parse("1.00", "XXX")
```

An unsupported currency code fails in the constructor and in `parse`.

```python
@pytest.mark.parametrize("minor", [1.5, "100", True])
def test_minor_units_must_be_an_integer(minor):
    with pytest.raises(InvalidMoneyError):
        Money(minor, "USD")
```

Only a real integer is accepted as an amount: a float, a text and a `bool` are refused.

```python
@pytest.mark.parametrize(
    ("minor", "currency", "text"),
    [(4250, "USD", "42.50"), (5, "USD", "0.05"), (0, "USD", "0.00"), (-123, "USD", "-1.23"), (500, "JPY", "500")],
)
```

Five (minor, currency, text) cases for `format`, including zero, a negative amount and yen.

```python
def test_format_uses_the_currencys_own_places(minor, currency, text):
    assert Money(minor, currency).format() == text
```

Each amount formats to the expected text.

```python
def test_arithmetic_and_comparison_stay_in_integers():
    a, b = Money(4250, "USD"), Money(123, "USD")
    assert a - b == Money(4127, "USD")
    assert a + b == Money(4373, "USD")
    assert -a == Money(-4250, "USD")
    assert b < a and b <= a and a > b and a >= b and not a < b
    assert a.is_positive() and not Money.zero("USD").is_positive() and Money.zero("USD").is_zero()
```

Subtraction, addition, negation and the four comparisons give exact integer results; `is_positive` and `is_zero` answer correctly.

```python
def test_combining_two_currencies_is_an_error():
    with pytest.raises(CurrencyMismatchError):
        Money(1, "USD") + Money(1, "EUR")
    with pytest.raises(CurrencyMismatchError):
        assert Money(1, "USD") < Money(1, "EUR")
```

Adding or comparing USD with EUR raises `CurrencyMismatchError`.

```python
def test_sum_money_of_nothing_is_zero_in_the_given_currency():
    assert sum_money([], "EUR") == Money(0, "EUR")
    assert sum_money([Money(1, "USD"), Money(2, "USD")], "USD") == Money(3, "USD")
```

The empty sum is zero in the requested currency; a normal sum adds up.

### `tests/unit/domain/test_ledger_and_signing.py` (73 lines)

```python
import pytest

from open_banking.domain.errors import InvalidIdempotencyKeyError, UnbalancedEntryError
from open_banking.domain.idempotency import IdempotencyKey, fingerprint
from open_banking.domain.ledger import Transfer
from open_banking.domain.money import Money
from open_banking.domain.signing import canonical_payment_request
```

Tests of the ledger transfer, the canonical signed request, idempotency keys and fingerprints.

```python
USD = "USD"
```

A shorthand.

```python
def test_a_transfer_is_two_postings_that_sum_to_zero():
    debit, credit = Transfer("acct-ada", "acct-shop", Money(3000, USD)).postings()
    assert (debit.account_id, debit.amount.minor) == ("acct-ada", -3000)
    assert (credit.account_id, credit.amount.minor) == ("acct-shop", 3000)
    assert debit.amount + credit.amount == Money(0, USD)
```

A transfer of 30.00 from Ada to the shop has a debit of -3000 and a credit of +3000, which add to zero.

```python
@pytest.mark.parametrize("minor", [0, -5])
```

A transfer must move a positive amount; 0 and -5 are refused.

```python
def test_a_transfer_moves_a_positive_amount(minor):
    with pytest.raises(UnbalancedEntryError):
        Transfer("a", "b", Money(minor, USD))
```

Each raises `UnbalancedEntryError`.

```python
def test_a_transfer_needs_two_different_accounts():
    with pytest.raises(UnbalancedEntryError):
        Transfer("a", "a", Money(1, USD))
```

The same account on both sides is refused.

```python
def request(**changes):
    values = {
        "client_id": "merchant-1",
        "consent_id": "c-1",
        "payee_account": "acct-shop",
        "amount": Money(3000, USD),
        "idempotency_key": "k-1",
    }
    return canonical_payment_request(**{**values, **changes})
```

A helper that builds the canonical request from default values, with any field changed by keyword (`{**values, **changes}` merges two dictionaries, the second winning).

```python
def test_the_signed_bytes_are_stable_and_name_every_field():
    assert request() == request()
    assert request() == (
        b'{"amount_minor":3000,"client_id":"merchant-1","consent_id":"c-1","currency":"USD",'
        b'"idempotency_key":"k-1","payee_account":"acct-shop"}'
    )
```

Two calls give identical bytes, and the bytes are exactly the sorted JSON with no spaces (two adjacent byte strings are joined).

```python
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
```

Six changes: another merchant, another consent, another payee, another amount, another currency, another idempotency key.

```python
def test_changing_any_field_changes_what_is_signed(change):
    assert request(**change) != request()
```

Each change gives different bytes, so a signature covers every field.

```python
@pytest.mark.parametrize("value", ["", "has space", "a" * 129, "né"])
```

Four bad idempotency keys: empty, containing a space, 129 characters, and a non-ASCII letter.

```python
def test_a_bad_idempotency_key_is_refused(value):
    with pytest.raises(InvalidIdempotencyKeyError):
        IdempotencyKey(value)
```

Each raises `InvalidIdempotencyKeyError`.

```python
def test_the_fingerprint_depends_on_every_field():
    base = fingerprint("initiate-payment", {"a": 1, "b": 2})
    assert base == fingerprint("initiate-payment", {"b": 2, "a": 1})
    assert base != fingerprint("initiate-payment", {"a": 1, "b": 3})
```

The fingerprint of a request does not depend on the order of its fields but changes when a value changes.

### `tests/unit/domain/test_consent_and_authentication.py` (200 lines)

```python
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
```

Tests of consent rules and of the authentication challenge. Imports bring in the domain types, the fixed start time and the `terms` helper. `USD` and `TTL` (a consent lifetime of 10 minutes) are constants.

```python
def proof_for(consent_terms: ConsentTerms, **overrides) -> AuthenticationProof:
    values = {
        "proof_id": "p1",
        "customer_id": "c1",
        "terms_digest": consent_terms.digest(),
        "expires_at": START + timedelta(minutes=2),
    }
    return AuthenticationProof(**{**values, **overrides})
```

Builds an `AuthenticationProof` for given terms, with overrides (`{**values, **overrides}`).

```python
def consent(consent_terms: ConsentTerms | None = None) -> Consent:
    consent_terms = consent_terms or terms()
    return Consent.grant("k1", proof_for(consent_terms), consent_terms, START, TTL)
```

Grants a consent from a valid proof (default terms).

```python
def request(amount: int = 3000, payee: str = "acct-shop") -> PaymentRequest:
    return PaymentRequest("k1", payee, Money(amount, USD))
```

A payment request under consent `k1` (default 30.00 to the shop).

```python
def test_a_consent_is_created_from_a_valid_proof_and_carries_its_scope_and_expiry():
    granted = consent()
    assert granted.terms.scope is ConsentScope.SINGLE_PAYMENT
    assert granted.expires_at == START + TTL and granted.customer_id == "c1"
    assert granted.revoked_at is None and granted.used_at is None
```

The consent has the single-payment scope, expires after the lifetime, belongs to the proof's customer and is neither revoked nor used.

```python
@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"consumed": True}, "already used"),
        ({"expires_at": START}, "expired"),
        ({"terms_digest": "0" * 64}, "different terms"),
    ],
)
```

Three bad proofs: already consumed, expired (expiry equal to now), and issued for other terms (a digest of zeros).

```python
def test_a_consent_cannot_be_created_from_a_proof_that_is_spent_expired_or_for_other_terms(override, message):
    consent_terms = terms()
    with pytest.raises(InvalidProofError, match=message):
        Consent.grant("k1", proof_for(consent_terms, **override), consent_terms, START, TTL)
```

Each raises `InvalidProofError` whose message contains the reason (`match=` is a regular expression searched in the message).

```python
def test_terms_need_a_positive_maximum_and_a_period_exactly_for_recurring_consents():
    with pytest.raises(InvalidProofError):
        terms(maximum=0)
    with pytest.raises(InvalidProofError):
        terms(scope=ConsentScope.RECURRING)
    with pytest.raises(InvalidProofError):
        terms(period=timedelta(days=30))
    assert terms(scope=ConsentScope.RECURRING, period=timedelta(days=30)).period == timedelta(days=30)
```

A zero maximum, a recurring consent without a period and a single-payment consent with a period are all refused; a recurring consent with a period is accepted.

```python
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
```

Six variants of the terms (payer, payee, merchant, maximum, scope and period, currency) each give a digest different from the base and from each other (a set of seven distinct values), while the same terms give the same digest.

```python
def test_revoking_sets_the_time_once_and_using_marks_it_used():
    first = consent().revoked(START + timedelta(minutes=1))
    assert first.revoked_at == START + timedelta(minutes=1)
    assert first.revoked(START + timedelta(minutes=5)) is first
    assert consent().marked_used(START).used_at == START
```

The first revocation sets the time; a second revocation returns the very same object (`is`); `marked_used` sets `used_at`.

```python
def refusal(granted: Consent, req: PaymentRequest, *, caller="merchant-1", at=START, spent=0) -> Refusal | None:
    return granted.refusal_for(req, caller, at, Money(spent, USD))
```

A helper asking the consent for its refusal with defaults for the caller, the time and the amount already spent.

```python
def test_a_payment_within_the_consent_has_no_refusal():
    assert refusal(consent(), request()) is None
    assert refusal(consent(), request(5000)) is None  # exactly the maximum is allowed
```

30.00 and exactly the maximum 50.00 are allowed.

```python
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
```

Six cases (wrong merchant, wrong payee, revoked, expired, already used, over the amount), each giving its own `Refusal` and its own message, for example `amount 60.00 exceeds consent max 50.00`.

```python
def test_the_consent_is_valid_up_to_but_not_including_its_expiry():
    granted = consent()
    assert refusal(granted, request(), at=START + TTL - timedelta(seconds=1)) is None
    assert refusal(granted, request(), at=START + TTL) is Refusal.CONSENT_EXPIRED
```

One second before expiry there is no refusal; at the expiry moment it is `CONSENT_EXPIRED`.

```python
def test_a_different_currency_is_not_covered():
    foreign = PaymentRequest("k1", "acct-shop", Money(3000, "EUR"))
    assert refusal(consent(), foreign) is Refusal.CURRENCY_NOT_COVERED
    assert "USD" in consent().explain(Refusal.CURRENCY_NOT_COVERED, foreign, Money(0, USD))
```

A EUR request under a USD consent gives `CURRENCY_NOT_COVERED` and the message mentions `USD`.

```python
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
```

A consent that is revoked, used and expired, asked with the wrong merchant, wrong payee and a too large amount: each time one condition is removed, the next refusal in the fixed order appears.

```python
def test_a_recurring_consent_caps_the_total_in_its_period_and_is_not_used_up():
    recurring = consent(terms(maximum=10_000, scope=ConsentScope.RECURRING, period=timedelta(days=30)))
    used = recurring.marked_used(START)
    assert refusal(used, request(6000), spent=0) is None
    assert refusal(used, request(5000), spent=6000) is Refusal.PERIOD_CAP_EXCEEDED
    assert refusal(used, request(4000), spent=6000) is None
    assert "total to 110.00, over the consent cap 100.00" in used.explain(
        Refusal.PERIOD_CAP_EXCEEDED, request(5000), Money(6000, USD)
    )
```

A recurring consent with a cap of 100.00 per 30 days: even when marked used it still allows payments; 60.00 with nothing spent is fine, 50.00 after 60.00 spent is `PERIOD_CAP_EXCEEDED`, 40.00 after 60.00 spent is fine; the message says the total would be 110.00, over the cap 100.00.

```python
def test_the_remaining_messages():
    granted = consent()
    spent = Money(0, USD)
    assert granted.explain(Refusal.CONSENT_NOT_FOUND, request(), spent) == "consent not found"
    assert "amount does not match approved consent" in granted.explain(Refusal.REQUEST_NOT_AS_SIGNED, request(), spent)
    assert granted.explain(Refusal.INSUFFICIENT_FUNDS, request(), spent) == "the payer has insufficient funds"
```

The messages of the refusals not covered above: consent not found, request not as signed, insufficient funds.

```python
def challenge() -> Challenge:
    return Challenge("c1", "u1", "t" * 64, "d" * 64, START, START + timedelta(minutes=5))
```

A helper: a fresh challenge that expires in 5 minutes.

```python
def test_the_right_code_verifies_the_challenge():
    updated, outcome = challenge().attempt(True, START)
    assert outcome is AttemptOutcome.VERIFIED and updated.status is ChallengeStatus.VERIFIED
```

A matching code gives `VERIFIED`.

```python
def test_wrong_codes_count_and_the_third_locks_the_challenge():
    current = challenge()
    for expected_attempts in range(1, MAX_ATTEMPTS):
        current, outcome = current.attempt(False, START)
        assert (outcome, current.attempts) == (AttemptOutcome.WRONG_CODE, expected_attempts)
    current, outcome = current.attempt(False, START)
    assert outcome is AttemptOutcome.LOCKED and current.status is ChallengeStatus.LOCKED
    again, outcome = current.attempt(True, START)  # even the right code is refused once locked
    assert outcome is AttemptOutcome.LOCKED and again is current
```

Wrong codes 1 and 2 give `WRONG_CODE` with the attempts counted; the third gives `LOCKED`; afterwards even the right code is refused as `LOCKED`, and the returned challenge is the very same object.

```python
def test_an_expired_or_used_challenge_accepts_nothing():
    assert challenge().attempt(True, START + timedelta(minutes=5))[1] is AttemptOutcome.EXPIRED
    verified, _ = challenge().attempt(True, START)
    assert verified.attempt(True, START)[1] is AttemptOutcome.ALREADY_USED
```

At the expiry moment a code is `EXPIRED`; a verified challenge gives `ALREADY_USED` on a second try.

### `tests/unit/application/test_access.py` (118 lines)

```python
"""The lesson's step 3: the merchant is authenticated with a secret and a token bound to its own key."""

from datetime import timedelta

import pytest

from open_banking.domain.errors import ClientAuthenticationError
from open_banking.infrastructure import keys
from tests.unit.conftest import BANK_URL
from tests.unit.helpers import dpop

TOKEN_URL = f"{BANK_URL}/oauth/token"
```

The lesson's step 3: the merchant is authenticated with a secret and a token bound to its own key. `TOKEN_URL` is the token endpoint's address.

```python
def issue(world, merchant=None, secret=None, proof=None):
    merchant = merchant or world.merchant
    proof = proof or dpop(merchant, world, "POST", "/oauth/token")
    return world.bank.issue_token.execute(merchant.client_id, secret or merchant.secret, proof, TOKEN_URL)
```

A helper: ask the bank for a token for a merchant (default `merchant-1`) with a secret and a proof, both overridable.

```python
def call(world, token, merchant=None, method="POST", path="/v1/payments", proof=None):
    merchant = merchant or world.merchant
    proof = proof or dpop(merchant, world, method, path, token)
    return world.bank.authenticate.execute(f"DPoP {token}", proof, method=method, url=f"{BANK_URL}{path}")
```

A helper: authenticate a request with a token and a proof (default: a fresh proof from the merchant's own key for that method and path).

```python
def test_a_registered_merchant_gets_a_token_and_is_recognized_with_a_fresh_proof(world):
    issued = issue(world)
    assert issued.expires_in_seconds == 600
    assert call(world, issued.access_token) == "merchant-1"
    assert call(world, issued.access_token) == "merchant-1"  # a new proof each time, so the same token works again
```

A token lasts 600 seconds and identifies `merchant-1`; with a new proof each time the same token works again.

```python
def test_only_a_digest_of_the_token_is_stored(world):
    issued = issue(world)
    assert issued.access_token not in world.db.tokens and len(world.db.tokens) == 1
```

The token itself is not among the stored keys of the token table (only its digest is), and there is exactly one.

```python
def test_a_wrong_secret_and_an_unknown_merchant_are_refused_alike(world):
    with pytest.raises(ClientAuthenticationError, match="invalid merchant credential"):
        issue(world, secret="not-the-secret")
    ghost = world.merchant.__class__("ghost", "x" * 20, world.merchant.private_pem, world.merchant.public_jwk)
    with pytest.raises(ClientAuthenticationError, match="invalid merchant credential"):
        world.bank.issue_token.execute("ghost", "x" * 20, dpop(ghost, world, "POST", "/oauth/token"), TOKEN_URL)
```

A wrong secret and an unknown merchant (`ghost`, built as another `Merchant` with the same key) both raise `ClientAuthenticationError` with the same message `invalid merchant credential`.

```python
def test_a_proof_signed_by_another_key_cannot_get_a_token(world):
    with pytest.raises(ClientAuthenticationError):
        issue(world, proof=dpop(world.other_merchant, world, "POST", "/oauth/token"))
```

A proof signed by the other merchant's key is refused.

```python
def test_a_proof_cannot_be_used_twice(world):
    proof = dpop(world.merchant, world, "POST", "/oauth/token")
    issue(world, proof=proof)
    with pytest.raises(ClientAuthenticationError, match="already used"):
        issue(world, proof=proof)
    token = issue(world).access_token
    request_proof = dpop(world.merchant, world, "POST", "/v1/payments", token)
    call(world, token, proof=request_proof)
    with pytest.raises(ClientAuthenticationError, match="already used"):
        call(world, token, proof=request_proof)
```

The same proof twice at the token endpoint, and the same proof twice on a request, are refused the second time with a message containing `already used`.

```python
def test_a_stolen_token_is_useless_without_the_merchants_key(world):
    token = issue(world).access_token
    with pytest.raises(ClientAuthenticationError, match="different key"):
        call(world, token, proof=dpop(world.other_merchant, world, "POST", "/v1/payments", token))
```

A request with the right token but a proof from the other merchant's key is refused (`different key`).

```python
def test_a_proof_for_another_request_or_token_is_refused(world):
    token = issue(world).access_token
    other_token = issue(world).access_token
    with pytest.raises(ClientAuthenticationError, match="different request"):
        call(world, token, proof=dpop(world.merchant, world, "GET", "/v1/payments", token))
    with pytest.raises(ClientAuthenticationError, match="different request"):
        call(world, token, proof=dpop(world.merchant, world, "POST", "/v1/elsewhere", token))
    with pytest.raises(ClientAuthenticationError, match="access token"):
        call(world, token, proof=dpop(world.merchant, world, "POST", "/v1/payments", other_token))
    with pytest.raises(ClientAuthenticationError, match="access token"):
        call(world, token, proof=dpop(world.merchant, world, "POST", "/v1/payments"))  # no token hash in the proof
    with pytest.raises(ClientAuthenticationError, match="does not carry"):
        issue(
            world, proof=dpop(world.merchant, world, "POST", "/oauth/token", token)
        )  # a token hash where none belongs
```

Five wrong proofs: another method, another path, another token's hash, no token hash at all, and a token hash on the token endpoint. Each is refused with its own message.

```python
def test_a_stale_proof_and_an_expired_token_are_refused(world):
    token = issue(world).access_token
    old = dpop(world.merchant, world, "POST", "/v1/payments", token)
    world.clock.advance(timedelta(seconds=61))
    with pytest.raises(ClientAuthenticationError, match="too old"):
        call(world, token, proof=old)
    world.clock.advance(timedelta(minutes=10))
    with pytest.raises(ClientAuthenticationError, match="invalid merchant credential"):
        call(world, token)
```

A proof 61 seconds old is `too old` (the limit is 60); after 10 more minutes the token has expired.

```python
@pytest.mark.parametrize("authorization", [None, "", "Bearer abc", "DPoP", "DPoP unknown-token"])
```

Five bad `Authorization` values: none, empty, the wrong scheme `Bearer`, no token after `DPoP`, and an unknown token.

```python
def test_a_missing_or_malformed_credential_is_refused(world, authorization):
    proof = dpop(world.merchant, world, "POST", "/v1/payments")
    with pytest.raises(ClientAuthenticationError):
        world.bank.authenticate.execute(authorization, proof, method="POST", url=f"{BANK_URL}/v1/payments")
```

Each raises `ClientAuthenticationError`.

```python
def test_a_request_without_a_proof_is_refused(world):
    token = issue(world).access_token
    with pytest.raises(ClientAuthenticationError):
        world.bank.authenticate.execute(f"DPoP {token}", None, method="POST", url=f"{BANK_URL}/v1/payments")
```

A token without a proof is refused.

```python
def test_a_token_whose_key_was_replaced_is_refused(world):
    token = issue(world).access_token
    _, new_jwk = keys.generate_keypair()
    world.db.clients["merchant-1"] = world.db.clients["merchant-1"].__class__(
        "merchant-1", "merchant-1", world.db.clients["merchant-1"].secret_hash, new_jwk
    )
    with pytest.raises(ClientAuthenticationError):
        call(world, token)
```

If the merchant's registered key is replaced after the token was issued, the token (bound to the old key's thumbprint) is refused. The test builds a new client record with a new key directly in the fake database.

### `tests/unit/application/test_authentication_and_consent.py` (147 lines)

```python
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
```

The lesson's step 2: a consent exists only after strong customer authentication, with a scope and an expiry. The imports bring in the outcome, scope and error types and the three helpers `authenticate`, `grant` and `terms`.

```python
def test_a_consent_exists_only_after_the_customer_passed_both_factors(world):
    consent = grant(world)
    assert consent.terms.scope is ConsentScope.SINGLE_PAYMENT
    assert consent.expires_at == world.clock.now() + timedelta(minutes=10)
    assert world.db.consents[consent.consent_id] is consent
```

`grant` does both steps (PIN, then code); the consent has the single-payment scope, expires exactly 10 minutes after the fixed clock, and is the stored object (`is` compares identity).

```python
def test_a_consent_cannot_be_created_without_a_proof_or_with_a_made_up_one(world):
    with pytest.raises(InvalidProofError, match="not known"):
        world.bank.grant_consent.execute("not-a-proof", terms())
    assert world.db.consents == {}
```

A made-up proof raises `InvalidProofError` mentioning `not known`; no consent is stored.

```python
def test_the_code_is_sent_to_the_phone_and_never_stored_in_clear(world):
    world.bank.start_challenge.execute("ada", world.ada_pin, terms())
    [(_, code, _)] = world.phone.messages
    stored = next(iter(world.db.challenges.values()))
    assert code not in stored.code_digest and len(stored.code_digest) == 64
    assert code not in repr(stored)
```

After starting a challenge, the phone has exactly one message (`[(_, code, _)] = ...` unpacks a list of one tuple, ignoring the other two fields). The stored challenge contains a 64-character digest that does not contain the code, and the code does not appear in the printed form of the challenge.

```python
def test_a_wrong_pin_and_an_unknown_user_get_the_same_answer(world):
    for username, pin in (("ada", "000000"), ("nobody", world.ada_pin)):
        with pytest.raises(AuthenticationFailedError, match="the username or PIN is not right"):
            world.bank.start_challenge.execute(username, pin, terms())
    assert world.phone.messages == [] and world.db.challenges == {}
```

A wrong PIN for `ada` and a right PIN for a user who does not exist both raise `AuthenticationFailedError` with the same message; no code is sent and no challenge is stored.

```python
def test_abandoned_authentication_leaves_no_consent(world):
    started = world.bank.start_challenge.execute("ada", world.ada_pin, terms())
    assert started.challenge_id in world.db.challenges
    assert world.db.consents == {} and world.db.proofs == {}
```

A started but never completed authentication leaves a challenge but no consent and no proof.

```python
def test_three_wrong_codes_lock_the_challenge_and_even_the_right_code_is_then_refused(world):
    started = world.bank.start_challenge.execute("ada", world.ada_pin, terms())
    results = [world.bank.verify_challenge.execute(started.challenge_id, "000000") for _ in range(3)]
    assert [r.outcome for r in results] == [AttemptOutcome.WRONG_CODE, AttemptOutcome.WRONG_CODE, AttemptOutcome.LOCKED]
    assert [r.attempts_left for r in results] == [2, 1, 0]
    right = world.phone.latest_code(world.db.customers_by_name("ada").customer_id)
    assert world.bank.verify_challenge.execute(started.challenge_id, right).outcome is AttemptOutcome.LOCKED
    assert world.db.proofs == {}
```

Three wrong codes give `WRONG_CODE`, `WRONG_CODE`, `LOCKED` with 2, 1 and 0 attempts left; then even the real code from the phone is `LOCKED`; no proof exists.

```python
def test_an_expired_challenge_and_a_challenge_used_twice_are_refused(world):
    started = world.bank.start_challenge.execute("ada", world.ada_pin, terms())
    code = world.phone.latest_code(world.db.customers_by_name("ada").customer_id)
    assert world.bank.verify_challenge.execute(started.challenge_id, code).outcome is AttemptOutcome.VERIFIED
    assert world.bank.verify_challenge.execute(started.challenge_id, code).outcome is AttemptOutcome.ALREADY_USED
    late = world.bank.start_challenge.execute("ada", world.ada_pin, terms())
    world.clock.advance(timedelta(minutes=5))
    code = world.phone.latest_code(world.db.customers_by_name("ada").customer_id)
    assert world.bank.verify_challenge.execute(late.challenge_id, code).outcome is AttemptOutcome.EXPIRED
```

The right code verifies once; entering it again is `ALREADY_USED`. A second challenge, after 5 minutes pass, is `EXPIRED`.

```python
def test_an_unknown_challenge_is_an_error(world):
    with pytest.raises(ChallengeNotFoundError):
        world.bank.verify_challenge.execute("00000000-0000-4000-8000-0000000000ff", "123456")
```

An unknown challenge id raises `ChallengeNotFoundError`.

```python
def test_a_proof_is_tied_to_the_terms_the_customer_saw(world):
    proof = authenticate(world, "ada", world.ada_pin, terms(maximum=5000))
    with pytest.raises(InvalidProofError, match="different terms"):
        world.bank.grant_consent.execute(proof, terms(maximum=500_000))
    assert world.db.consents == {}
    assert world.bank.grant_consent.execute(proof, terms(maximum=5000)).terms.max_amount.minor == 5000
```

A proof earned for a 50.00 maximum cannot create a consent for 5,000.00 (`different terms`) and no consent is stored; used with the original terms it works.

```python
def test_a_proof_is_good_for_one_consent_and_for_two_minutes(world):
    proof = authenticate(world, "ada", world.ada_pin, terms())
    world.bank.grant_consent.execute(proof, terms())
    with pytest.raises(InvalidProofError, match="already used"):
        world.bank.grant_consent.execute(proof, terms())
    stale = authenticate(world, "ada", world.ada_pin, terms())
    world.clock.advance(timedelta(minutes=2))
    with pytest.raises(InvalidProofError, match="expired"):
        world.bank.grant_consent.execute(stale, terms())
```

Using a proof twice is `already used`; a proof used after 2 minutes is `expired`.

```python
def test_a_customer_can_only_approve_payments_from_their_own_account(world):
    with pytest.raises(AccountNotOwnedError):
        world.bank.start_challenge.execute("ada", world.ada_pin, terms(payer="acct-bob"))
```

Ada naming Bob's account as the payer raises `AccountNotOwnedError`.

```python
def test_the_merchant_and_the_payee_must_exist(world):
    with pytest.raises(UnknownClientError):
        world.bank.start_challenge.execute("ada", world.ada_pin, terms(client="merchant-9"))
    with pytest.raises(AccountNotFoundError):
        world.bank.start_challenge.execute("ada", world.ada_pin, terms(payee="acct-nowhere"))
    proof = authenticate(world, "ada", world.ada_pin, terms())
    del world.db.clients["merchant-1"]
    with pytest.raises(UnknownClientError):
        world.bank.grant_consent.execute(proof, terms())
```

An unknown merchant and an unknown payee account are refused when starting the challenge; a merchant deleted after authentication is refused when granting.

```python
def test_a_recurring_consent_lives_ninety_days_and_a_single_payment_ten_minutes(world):
    recurring = grant(world, terms(scope=ConsentScope.RECURRING, period=timedelta(days=30)))
    single = grant(world)
    assert recurring.expires_at - recurring.created_at == timedelta(days=90)
    assert single.expires_at - single.created_at == timedelta(minutes=10)
```

The two lifetimes.

```python
def test_the_customer_can_revoke_and_revoking_twice_changes_nothing(world):
    consent = grant(world)
    first = world.bank.revoke_consent.execute("ada", world.ada_pin, consent.consent_id)
    world.clock.advance(timedelta(minutes=1))
    second = world.bank.revoke_consent.execute("ada", world.ada_pin, consent.consent_id)
    assert first.revoked_at == second.revoked_at is not None
```

A second revocation a minute later keeps the first revocation time (`first.revoked_at == second.revoked_at is not None` is a chained comparison).

```python
def test_only_the_owner_with_the_right_pin_can_revoke(world):
    consent = grant(world)
    with pytest.raises(AuthenticationFailedError):
        world.bank.revoke_consent.execute("ada", "000000", consent.consent_id)
    with pytest.raises(ConsentNotFoundError):
        world.bank.revoke_consent.execute("bob", world.bob_pin, consent.consent_id)
    with pytest.raises(ConsentNotFoundError):
        world.bank.revoke_consent.execute("ada", world.ada_pin, "00000000-0000-4000-8000-0000000000ff")
    assert world.db.consents[consent.consent_id].revoked_at is None
```

A wrong PIN is `AuthenticationFailedError`; another customer, or an unknown consent id, gives `ConsentNotFoundError` (the same answer, so existence is not revealed); the consent stays unrevoked.

```python
def test_enrolling_and_registering_refuse_unknown_accounts_and_store_only_hashes(world):
    with pytest.raises(AccountNotFoundError):
        world.bank.enroll_customer.execute("carol", "654321", "acct-nowhere")
    stored = world.db.customers_by_name("ada")
    assert world.ada_pin not in stored.pin_hash and stored.pin_hash.startswith("scrypt$")
    assert world.merchant.secret not in world.db.clients["merchant-1"].secret_hash
```

Enrolling a customer on an unknown account is refused; the stored PIN hash does not contain the PIN and starts with `scrypt$`; the merchant secret is not in its stored hash.

### `tests/unit/application/test_merchant_checkout.py` (89 lines)

```python
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
```

The merchant's side: a checkout is completed with the customer's consent id, and a retry is always safe.

```python
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
```

A fake bank gateway. It is given a list of answers; each call records its arguments and takes the next answer, raising it if it is an exception (`list.pop(0)` removes the first item).

```python
def parts(*answers):
    checkouts = InMemoryCheckouts()
    gateway = ScriptedGateway(*answers)
    create = CreateCheckout(checkouts, SequentialIds())
    complete = CompleteCheckout(checkouts, gateway, MerchantProfile("merchant-1", "acct-shop"))
    return create, complete, gateway, checkouts
```

Builds the pieces under test around a scripted gateway: in-memory checkouts, `CreateCheckout` with sequential ids, and `CompleteCheckout` for the merchant `merchant-1` paid into `acct-shop`. `*answers` collects any number of positional arguments.

```python
def test_a_checkout_starts_awaiting_the_customers_consent():
    create, _, _, checkouts = parts()
    checkout = create.execute(Money(3000, "USD"))
    assert checkout.status is CheckoutStatus.AWAITING_CONSENT and checkouts.get(checkout.checkout_id) == checkout
```

A new checkout has the status `AWAITING_CONSENT` and is stored.

```python
def test_completing_with_a_consent_pays_and_asks_the_bank_for_the_merchants_own_payee():
    create, complete, gateway, _ = parts(Paid("p-1"))
    checkout = create.execute(Money(3000, "USD"))
    paid = complete.execute(checkout.checkout_id, "c-1")
    assert (paid.status, paid.payment_id, paid.consent_id) == (CheckoutStatus.PAID, "p-1", "c-1")
    assert gateway.calls == [("c-1", "acct-shop", Money(3000, "USD"), f"checkout-{checkout.checkout_id}-c-1")]
```

Completing with consent `c-1` returns a paid checkout with payment id `p-1`; the gateway received the consent id, the merchant's own payee account, the amount and the key `checkout-<id>-c-1`.

```python
def test_completing_a_paid_checkout_again_pays_nothing_more():
    create, complete, gateway, _ = parts(Paid("p-1"))
    checkout = create.execute(Money(3000, "USD"))
    first = complete.execute(checkout.checkout_id, "c-1")
    assert complete.execute(checkout.checkout_id, "c-1") == first and len(gateway.calls) == 1
```

A second completion returns the same checkout and the gateway was called once.

```python
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
```

A refusal leaves the checkout `AWAITING_CONSENT` with the bank's reason; completing with another consent pays and clears the refusal; the two calls used different keys.

```python
def test_when_the_bank_cannot_be_reached_the_outcome_is_unknown_and_the_retry_uses_the_same_key():
    create, complete, gateway, checkouts = parts(BankUnavailableError("no answer"), Paid("p-1"))
    checkout = create.execute(Money(3000, "USD"))
    with pytest.raises(BankUnavailableError):
        complete.execute(checkout.checkout_id, "c-1")
    stored = checkouts.get(checkout.checkout_id)
    assert isinstance(stored, Checkout) and stored.status is CheckoutStatus.UNKNOWN
    assert complete.execute(checkout.checkout_id, "c-1").status is CheckoutStatus.PAID
    assert gateway.calls[0][3] == gateway.calls[1][3]
```

`BankUnavailableError` is raised to the caller, the stored checkout is `UNKNOWN`, and the retry with the same consent pays and uses the **same** key as the first attempt.

```python
def test_an_unknown_checkout_is_an_error():
    _, complete, _, _ = parts()
    with pytest.raises(CheckoutNotFoundError):
        complete.execute("nope", "c-1")
```

Completing a checkout that does not exist raises `CheckoutNotFoundError`.

### `tests/unit/application/test_payments.py` (268 lines)

```python
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
```

The lesson's steps 4 to 6: money moves only under a valid consent, once, for the signed amount. `threading` runs code in several threads; the imports include Hypothesis (`given`, `settings`, `strategies`), used for the property test at the end.

```python
def refused(world, consent_id, amount, key, **kwargs) -> PaymentRefusedError:
    with pytest.raises(PaymentRefusedError) as caught:
        pay(world, consent_id, amount, key, **kwargs)
    return caught.value
```

A helper: make a payment that must be refused and return the error. `pytest.raises(...) as caught` keeps the raised error in `caught.value`.

```python
def test_a_payment_within_the_consent_moves_exactly_that_money_and_balances_the_ledger(world):
    consent = grant(world)
    response, replayed = pay(world, consent.consent_id, 3000, "k-1")
    assert response.status == 201 and not replayed
    assert response.body["status"] == "POSTED" and response.body["amount_minor"] == 3000
    assert (world.db.balance("acct-ada"), world.db.balance("acct-shop")) == (47_000, 3000)
    assert world.db.ledger_sum() == 0
    assert world.db.consents[consent.consent_id].used_at is not None
```

A 30.00 payment: status 201, not a replay, `POSTED`, 3000 minor units; Ada has 47,000, the shop 3,000, the ledger sums to zero, and the single-payment consent is marked used.

```python
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
```

60.00 against 50.00: `amount_exceeds_consent` with the exact message; another payee: `payee_not_covered`; after 10 minutes and 1 second: `consent_expired`. Ada's balance is unchanged and the ledger sums to zero.

```python
def test_a_refusal_changes_nothing_and_stores_nothing(world):
    consent = grant(world)
    refused(world, consent.consent_id, 6000, "k-over")
    assert world.db.payments == {} and world.db.keys == {}
    response, _ = pay(world, consent.consent_id, 3000, "k-over")  # the same key, now a good request, works
    assert response.status == 201
```

After a refusal there is no payment and no stored key; the same key with a good request then works (a refused attempt does not burn the key).

```python
def test_a_replay_with_the_same_key_returns_the_stored_answer_and_moves_no_money(world):
    consent = grant(world)
    first, first_replayed = pay(world, consent.consent_id, 3000, "k-1")
    second, second_replayed = pay(world, consent.consent_id, 3000, "k-1")
    assert (first_replayed, second_replayed) == (False, True)
    assert first == second and len(world.db.payments) == 1
    assert world.db.balance("acct-ada") == 47_000
```

The same request twice: not replayed then replayed, equal answers, one payment, and Ada debited once.

```python
def test_a_replay_still_works_after_the_consent_was_revoked_because_the_money_already_moved(world):
    consent = grant(world)
    first, _ = pay(world, consent.consent_id, 3000, "k-1")
    world.bank.revoke_consent.execute("ada", world.ada_pin, consent.consent_id)
    second, replayed = pay(world, consent.consent_id, 3000, "k-1")
    assert replayed and second == first and len(world.db.payments) == 1
```

After the consent is revoked, repeating the paid request still returns the stored answer (it reports what happened) and no second payment exists.

```python
def test_a_key_reused_for_a_different_request_is_refused_not_replayed(world):
    consent = grant(world, terms(maximum=10_000, scope=ConsentScope.RECURRING, period=timedelta(days=30)))
    pay(world, consent.consent_id, 3000, "k-1")
    with pytest.raises(IdempotencyKeyReusedError):
        pay(world, consent.consent_id, 3100, "k-1")
    assert len(world.db.payments) == 1
```

A recurring consent: the same key with 31.00 instead of 30.00 raises `IdempotencyKeyReusedError`; still one payment.

```python
def test_two_merchants_may_use_the_same_key(world):
    own = grant(world)
    other_terms = terms(client="merchant-2", payee="acct-other-shop")
    theirs = grant(world, other_terms)
    first, _ = pay(world, own.consent_id, 3000, "same-key")
    second, replayed = pay(
        world, theirs.consent_id, 3000, "same-key", merchant=world.other_merchant, payee="acct-other-shop"
    )
    assert not replayed and first.body["payment_id"] != second.body["payment_id"]
```

Keys are scoped to the merchant: the same key text from `merchant-2` is not a replay and yields a different payment.

```python
def test_a_changed_amount_is_detected_because_the_signature_no_longer_matches(world):
    consent = grant(world)
    error = refused(world, consent.consent_id, 3500, "k-1", signed_minor=3000)
    assert error.refusal == "request_not_as_signed"
    assert error.message == "amount does not match approved consent: the request signature does not cover this request"
    assert world.db.payments == {} and world.db.balance("acct-ada") == 50_000
```

Sending 35.00 while the signature covers 30.00 is `request_not_as_signed` with the lesson's message; nothing is stored and no money moves.

```python
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
```

No signature, and a signature made by another merchant's key, both give `request_not_as_signed`.

```python
def test_a_signature_does_not_move_to_another_attempt(world):
    consent = grant(world)
    signature = sign_request(world.merchant, consent.consent_id, "acct-shop", Money(3000, "USD"), "k-1")
    request = PaymentRequest(consent.consent_id, "acct-shop", Money(3000, "USD"))
    with pytest.raises(PaymentRefusedError, match="signature"):
        world.bank.initiate_payment.execute("merchant-1", IdempotencyKey("k-2"), request, signature)
```

A signature made for key `k-1` is refused when sent with key `k-2`, because the key is part of the signed bytes.

```python
def test_another_merchants_consent_is_refused_even_though_it_exists_and_is_valid(world):
    consent = grant(world)
    error = refused(world, consent.consent_id, 3000, "k-1", merchant=world.other_merchant)
    assert error.refusal == "consent_not_granted_to_caller"
    assert world.db.consents[consent.consent_id].used_at is None
```

`merchant-2` presenting `merchant-1`'s consent gets `consent_not_granted_to_caller`, and the consent is still unused.

```python
def test_an_unknown_consent_is_not_found(world):
    error = refused(world, "00000000-0000-4000-8000-0000000000ff", 3000, "k-1")
    assert (error.refusal, error.message) == ("consent_not_found", "consent not found")
```

`consent_not_found` with its message.

```python
def test_a_revoked_consent_moves_no_money(world):
    consent = grant(world)
    world.bank.revoke_consent.execute("ada", world.ada_pin, consent.consent_id)
    error = refused(world, consent.consent_id, 3000, "k-1")
    assert (error.refusal, error.message) == ("consent_revoked", "consent revoked")
    assert world.db.balance("acct-ada") == 50_000
```

`consent_revoked` and Ada's balance is unchanged.

```python
def test_a_single_payment_consent_pays_once(world):
    consent = grant(world)
    pay(world, consent.consent_id, 3000, "k-1")
    assert refused(world, consent.consent_id, 1000, "k-2").refusal == "consent_already_used"
```

A second payment with a new key is `consent_already_used`.

```python
def test_the_payer_cannot_be_overdrawn(world):
    consent = grant(world, terms(maximum=90_000))
    error = refused(world, consent.consent_id, 60_000, "k-1")
    assert error.refusal == "insufficient_funds" and world.db.balance("acct-ada") == 50_000
```

A consent of 900.00 with a 600.00 payment from an account holding 500.00 is `insufficient_funds`; the balance is unchanged.

```python
def test_a_recurring_consent_enforces_its_cap_over_the_period_and_then_the_window_moves(world):
    consent = grant(world, terms(maximum=10_000, scope=ConsentScope.RECURRING, period=timedelta(days=30)))
    pay(world, consent.consent_id, 6000, "r-1")
    error = refused(world, consent.consent_id, 5000, "r-2")
    assert error.refusal == "period_cap_exceeded" and "110.00" in error.message
    pay(world, consent.consent_id, 4000, "r-3")  # exactly the cap
    world.clock.advance(timedelta(days=31))
    pay(world, consent.consent_id, 5000, "r-4")
    assert world.db.balance("acct-ada") == 50_000 - 6000 - 4000 - 5000 and world.db.ledger_sum() == 0
```

Cap 100.00 per 30 days: 60.00 pays; 50.00 more is `period_cap_exceeded` (the message contains 110.00); 40.00 pays (exactly the cap); after 31 days 50.00 pays; the balance is 500 - 60 - 40 - 50 and the ledger sums to zero.

```python
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
```

Twenty threads call with one key; a `threading.Barrier(20)` releases them together. All answers are equal, exactly one is not a replay, one payment exists and Ada is debited once.

```python
def test_a_payment_is_read_back_only_by_the_merchant_that_made_it(world):
    consent = grant(world)
    response, _ = pay(world, consent.consent_id, 3000, "k-1")
    payment_id = response.body["payment_id"]
    assert world.bank.get_payment.execute("merchant-1", payment_id)["amount_minor"] == 3000
    with pytest.raises(PaymentNotFoundError):
        world.bank.get_payment.execute("merchant-2", payment_id)
    with pytest.raises(PaymentNotFoundError):
        world.bank.get_payment.execute("merchant-1", "00000000-0000-4000-8000-0000000000ff")
```

`get_payment` returns the payment for its own merchant; another merchant, or an unknown id, gets `PaymentNotFoundError`.

```python
def test_a_payment_from_an_unregistered_caller_is_refused(world):
    consent = grant(world)
    request = PaymentRequest(consent.consent_id, "acct-shop", Money(3000, "USD"))
    with pytest.raises(ClientAuthenticationError):
        world.bank.initiate_payment.execute("merchant-9", IdempotencyKey("k-1"), request, "sig")
```

An unregistered merchant id raises `ClientAuthenticationError`.

```python
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
```

`monkeypatch` is a pytest fixture that temporarily replaces attributes. The payment repository's `add` is replaced by a function that raises `RuntimeError`; the payment attempt raises it; afterwards Ada's balance, the key store and the payments are untouched (the fake unit of work rolled back), and the same key then works.

```python
def test_balances_and_the_ledger_sum_are_readable(world):
    balances, total = world.bank.balances.execute(["acct-ada", "acct-shop"])
    assert (balances["acct-ada"].minor, balances["acct-shop"].minor, total.minor) == (50_000, 0, 0)
    assert world.bank.balances.counts().consents == 0
    from open_banking.domain.errors import AccountNotFoundError

    with pytest.raises(AccountNotFoundError):
        world.bank.balances.execute(["acct-nowhere"])
```

The balances use case returns the balances and a zero total; the consent count is zero; an unknown account raises `AccountNotFoundError`.

```python
OPERATIONS = st.sampled_from(["grant", "pay", "pay_again", "revoke", "tick", "tamper", "other_merchant"])
```

A Hypothesis strategy that picks one operation name at random: grant, pay, pay again with a reused key, revoke, tick the clock, tamper with the signed amount, or use another merchant.

```python
@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture]
)
```

`max_examples=40` runs 40 generated sequences; `deadline=None` switches off the per-example time limit; `suppress_health_check` silences two Hypothesis warnings (generation is slow; a function-scoped fixture is reused between examples).

```python
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
```

The **property test**. `@given(steps=...)` generates lists of up to 25 (operation, number) pairs. Each test run builds a fresh world and walks the steps: grant a consent whose maximum is the number; pay or pay again (the consent and the key are chosen from the number); revoke; advance the clock by 0 to 14 minutes; send a tampered request (the signature covers an amount one cent larger than the amount sent); or pay as the other merchant. Refusals (`PaymentRefusedError`, `IdempotencyKeyReusedError`) are caught and ignored because they are part of the story. After **every** step the ledger sums to zero and Ada's balance is not negative. At the end: Ada's and the shop's balances together are still 500.00, and every payment that exists was within its consent's maximum, before its expiry, by the consent's own merchant, and not after the consent's revocation; all payment ids are distinct. The imports inside the function avoid a circular import at module load.

### `tests/unit/infrastructure/test_crypto_and_keys.py` (157 lines)

```python
import json
import os
import stat
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from open_banking.domain.errors import ClientAuthenticationError
from open_banking.infrastructure import keys
from open_banking.infrastructure.crypto import StandardCrypto
```

Tests of the cryptography adapter and the key helpers, using the **real** libraries. New imports: `json`, `os`, `stat` (reads file permissions), the time types, PyJWT (`jwt`), and the project's `keys` and `StandardCrypto`.

```python
NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
URL = "http://bank.test/v1/payments"
PEM, JWK = keys.generate_keypair()
OTHER_PEM, OTHER_JWK = keys.generate_keypair()
```

Fixed constants: a moment in time (`NOW`), a URL (`URL`) and two key pairs generated once for the whole file (`PEM, JWK = keys.generate_keypair()` unpacks the private key and the public JWK; `OTHER_...` is a second pair for "another key" tests).

```python
@pytest.fixture
def crypto():
    return StandardCrypto(pepper="test-pepper-0123456789", scrypt_n=16)
```

A fixture named `crypto` giving a `StandardCrypto` with a test pepper and a tiny scrypt cost (16) so the tests are fast.

```python
def proof(private=PEM, public=JWK, method="POST", url=URL, now=NOW, token=None):
    return keys.build_dpop_proof(private, public, method=method, url=url, now=now, access_token=token)
```

A helper that builds a DPoP proof with defaults (the main key, POST, the test URL, `NOW`, no token), each overridable.

```python
def verify(crypto, text, method="POST", url=URL, now=NOW, token=None, public=JWK):
    return crypto.verify_dpop_proof(text, public, method=method, url=url, now=now, access_token=token)
```

A helper that verifies a proof with the same defaults.

```python
def test_a_secret_is_stored_as_a_salted_slow_hash_and_checked_in_constant_time(crypto):
    first, second = crypto.hash_secret("246810"), crypto.hash_secret("246810")
    assert first != second and first.startswith("scrypt$16$8$1$") and "246810" not in first
    assert crypto.verify_secret("246810", first) and not crypto.verify_secret("246811", first)
```

Hashing the same PIN twice gives two different strings (different salts), both starting with `scrypt$16$8$1$` and neither containing the PIN; the right secret verifies and a near miss does not.

```python
@pytest.mark.parametrize("stored", ["", "plain", "scrypt$x$8$1$aa$bb", "bcrypt$16$8$1$aa$bb", "scrypt$16$8$1$aa"])
```

Five malformed stored values: empty, no `$`, a non-numeric cost, another scheme name and too few parts.

```python
def test_a_malformed_stored_secret_is_simply_not_a_match(crypto, stored):
    assert crypto.verify_secret("anything", stored) is False
```

Each gives exactly `False` (`is False`), never an exception.

```python
def test_the_cost_comes_from_the_stored_value_so_it_can_change_over_time():
    cheap, dearer = StandardCrypto(pepper="p" * 16, scrypt_n=16), StandardCrypto(pepper="p" * 16, scrypt_n=32)
    assert dearer.verify_secret("s", cheap.hash_secret("s"))
```

A hash made at cost 16 verifies with an object configured for cost 32, because the cost is read from the stored text.

```python
def test_digests_are_keyed_by_the_pepper_and_tied_to_what_they_protect(crypto):
    other = StandardCrypto(pepper="another-pepper-0123456789", scrypt_n=16)
    digest = crypto.code_digest("challenge-1", "123456")
    assert digest == crypto.code_digest("challenge-1", "123456")
    assert digest != crypto.code_digest("challenge-2", "123456") != other.code_digest("challenge-2", "123456")
    assert digest != other.code_digest("challenge-1", "123456")
    assert crypto.token_digest("t") != other.token_digest("t") and crypto.token_digest("t") != crypto.code_digest(
        "t", ""
    )
```

A code digest is repeatable, differs for another challenge, another code or another pepper, and a token digest differs from a code digest (the chained `!=` checks all neighbors).

```python
def test_codes_are_six_digits_and_tokens_are_unguessable(crypto):
    codes = {crypto.new_code() for _ in range(50)}
    assert all(len(code) == 6 and code.isdigit() for code in codes) and len(codes) > 25
    assert len({crypto.new_token() for _ in range(50)}) == 50 and len(crypto.new_token()) >= 40
```

50 codes are all six digits and show variety (more than 25 distinct); 50 tokens are all different and at least 40 characters long.

```python
def test_a_valid_proof_verifies_and_reports_its_id_and_time(crypto):
    claims = verify(crypto, proof())
    assert claims.issued_at == NOW and len(claims.jti) == 32
    assert verify(crypto, proof()).jti != claims.jti
```

A good proof verifies: the issue time equals `NOW` and the id is 32 characters; a second proof has another id.

```python
def test_a_proof_for_a_token_carries_the_hash_of_that_token(crypto):
    assert verify(crypto, proof(token="abc"), token="abc")
    with pytest.raises(ClientAuthenticationError, match="access token"):
        verify(crypto, proof(token="abc"), token="abd")
```

A proof made for token `abc` verifies against `abc` and is refused for `abd`.

```python
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"method": "GET"}, "different request"),
        ({"url": "http://bank.test/v1/other"}, "different request"),
        ({"url": "http://evil.test/v1/payments"}, "different request"),
        ({"now": NOW - timedelta(seconds=61)}, "too old"),
        ({"now": NOW + timedelta(seconds=61)}, "too old"),
    ],
)
```

Five variations: another method, another path, another host, a proof 61 seconds in the past and one 61 seconds in the future.

```python
def test_a_proof_for_another_request_or_time_is_refused(crypto, change, message):
    made = proof(**{k: v for k, v in change.items() if k in {"method", "url", "now"}})
    checked = {"method": "POST", "url": URL, "now": NOW}
    if "now" in change:
        made = proof()
        checked["now"] = change["now"]
    with pytest.raises(ClientAuthenticationError, match=message):
        verify(crypto, made, **checked)
```

The proof is built for the changed request (or, for the time cases, the check is done with a shifted `now`) and verified against the original; each raises `ClientAuthenticationError` with the expected message.

```python
def test_the_query_string_is_not_part_of_what_a_proof_is_bound_to(crypto):
    assert verify(crypto, proof(url=URL + "?a=1"), url=URL + "?b=2")
```

Two URLs differing only in the query string both verify.

```python
def test_a_proof_signed_by_another_key_is_refused(crypto):
    with pytest.raises(ClientAuthenticationError, match="different key"):
        verify(crypto, proof(OTHER_PEM, OTHER_JWK))
    forged = proof(OTHER_PEM, JWK)  # claims our key in its header but is signed with another
    with pytest.raises(ClientAuthenticationError):
        verify(crypto, forged)
```

A proof signed and labeled with another key is refused (`different key`); a proof that claims our key in its header but is signed with another key fails the signature check.

```python
def test_a_tampered_or_malformed_proof_is_refused(crypto):
    header, payload, signature = proof().split(".")
    claims = json.loads(keys.b64url_decode(payload))
    claims["htm"] = "GET"
    tampered = ".".join([header, keys.b64url(json.dumps(claims).encode()), signature])
    for bad in (tampered, "not.a.jwt", "", proof()[:-4]):
        with pytest.raises(ClientAuthenticationError):
            verify(crypto, bad)
```

A proof is three dot-separated parts (header, payload, signature). The test changes the method in the payload and re-encodes it with the old signature, and also tries `not.a.jwt`, an empty string and a proof with its end cut off; each is refused.

```python
def test_the_wrong_type_algorithm_or_missing_id_is_refused(crypto):
    plain = jwt.encode({"htm": "POST", "htu": URL, "iat": int(NOW.timestamp()), "jti": "x"}, PEM, algorithm="ES256")
    no_id = jwt.encode(
        {"htm": "POST", "htu": URL, "iat": int(NOW.timestamp())}, PEM, algorithm="ES256", headers={"typ": "dpop+jwt"}
    )
    text_time = jwt.encode(
        {"htm": "POST", "htu": URL, "iat": "yesterday", "jti": "x"}, PEM, algorithm="ES256", headers={"typ": "dpop+jwt"}
    )
    for bad in (plain, no_id, text_time):
        with pytest.raises(ClientAuthenticationError):
            verify(crypto, bad)
```

Three well-signed but wrong proofs: without the `dpop+jwt` type header, without an id, and with a text instead of a number as the time. Each is refused.

```python
def test_request_signatures_verify_only_for_the_signed_bytes_and_key(crypto):
    signature = keys.sign(PEM, b"amount=3000")
    assert crypto.verify_signature(JWK, b"amount=3000", signature)
    assert not crypto.verify_signature(JWK, b"amount=3500", signature)
    assert not crypto.verify_signature(OTHER_JWK, b"amount=3000", signature)
    assert not crypto.verify_signature(JWK, b"amount=3000", "not-a-signature")
    assert not crypto.verify_signature({"kty": "EC"}, b"amount=3000", signature)
```

A signature over `amount=3000` verifies for those bytes and that key; it fails for other bytes, for another key, for garbage text and for a malformed key.

```python
def test_thumbprints_identify_a_key_independent_of_member_order(crypto):
    assert crypto.thumbprint(JWK) == crypto.thumbprint(dict(reversed(list(JWK.items()))))
    assert crypto.thumbprint(JWK) != crypto.thumbprint(OTHER_JWK) and len(crypto.thumbprint(JWK)) == 43
```

The thumbprint is the same when the key's members are listed in reverse order, differs for another key, and is 43 characters (a base64url SHA-256).

```python
def test_a_private_key_file_is_readable_by_its_owner_only(tmp_path):
    path = tmp_path / "nested" / "key.pem"
    keys.write_private_key(path, PEM)
    assert keys.read_private_key(path) == PEM and keys.public_jwk_of(PEM) == JWK
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert set(JWK) == {"kty", "crv", "x", "y"} and "d" not in JWK
```

`tmp_path` is a pytest fixture giving a new temporary folder. Writing a key into a nested folder creates the folders, reading it back gives the same text, the public JWK derived from it equals the original, the file permission is exactly `0o600`, and the public JWK has only `kty`, `crv`, `x` and `y` (no private member `d`).

```python
def test_base64url_round_trips_without_padding():
    for size in range(0, 9):
        data = bytes(range(size))
        assert "=" not in keys.b64url(data) and keys.b64url_decode(keys.b64url(data)) == data
```

For lengths 0 to 8 (`bytes(range(size))`), the encoding contains no `=` and decoding gives back the same bytes.

### `tests/unit/infrastructure/test_settings_otp_faults.py` (111 lines)

```python
import json
import stat
from pathlib import Path

import httpx
import pytest

from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.faults import Fault, FaultInjectingTransport, FaultScript
from open_banking.infrastructure.otp import FileOtpChannel, MemoryOtpChannel
from open_banking.infrastructure.settings import ConfigurationError, MerchantConfig

BANK = {"POSTGRES_PASSWORD": "not-a-real-secret", "BANK_PEPPER": "pepper-0123456789abc"}
MERCHANT = {"MERCHANT_CLIENT_SECRET": "not-a-real-secret"}
```

Tests of the settings, the one-time-code channels and the scripted network faults. `BANK` and `MERCHANT` are minimal environments with the required secrets.

```python
def test_the_bank_defaults_give_a_working_local_configuration_and_hide_secrets():
    config = BankConfig.from_env(BANK)
    assert config.server.port == 8463 and config.public_url == "http://127.0.0.1:8463"
    assert config.postgres.port == 54613 and config.timings.consent_time_to_live.total_seconds() == 600
    assert config.timings.recurring_consent_time_to_live.days == 90 and config.scrypt_n == 2**14
    assert "not-a-real-secret" not in repr(config) and "pepper-0123456789abc" not in repr(config)
```

With only the required secrets the bank listens on 8463 with the public URL `http://127.0.0.1:8463`, uses database port 54613, a 600-second consent lifetime, 90-day recurring consents and scrypt cost 2**14; neither the password nor the pepper appears in the printed form.

```python
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BANK_PEPPER", "short"),
        ("BANK_PORT", "0"),
        ("BANK_PUBLIC_URL", "bank.test"),
        ("SCRYPT_N", "1000"),
        ("LOG_LEVEL", "LOUD"),
        ("CONSENT_MINUTES", "-1"),
    ],
)
```

Six bad values: a short pepper, port 0, a URL without a scheme, a scrypt cost that is not a power of two, a bad log level and a negative consent lifetime.

```python
def test_a_bad_bank_value_names_the_variable(name, value):
    with pytest.raises(ConfigurationError, match=name):
        BankConfig.from_env({**BANK, name: value})
```

Each raises `ConfigurationError` whose message names the variable.

```python
def test_the_bank_secrets_have_no_defaults():
    for name in ("POSTGRES_PASSWORD", "BANK_PEPPER"):
        with pytest.raises(ConfigurationError, match=name):
            BankConfig.from_env({key: value for key, value in BANK.items() if key != name})
```

Removing `POSTGRES_PASSWORD` or `BANK_PEPPER` from the environment raises an error naming it (a dictionary comprehension builds the environment without one key).

```python
def test_the_merchant_needs_a_secret_and_has_no_database_settings():
    config = MerchantConfig.from_env(MERCHANT)
    assert config.client_id == "merchant-1" and config.attempts == 3 and config.server.port == 8464
    assert "not-a-real-secret" not in repr(config) and not hasattr(config, "postgres")
    with pytest.raises(ConfigurationError, match="MERCHANT_CLIENT_SECRET"):
        MerchantConfig.from_env({})
    with pytest.raises(ConfigurationError, match="BANK_URL"):
        MerchantConfig.from_env({**MERCHANT, "BANK_URL": "bank"})
```

The merchant configuration has its defaults (client `merchant-1`, 3 attempts, port 8464), hides the secret, has no `postgres` attribute at all (`hasattr`); a missing secret and a bad `BANK_URL` are each named in the error.

```python
def test_the_phone_file_keeps_each_code_and_is_private(tmp_path):
    phone = FileOtpChannel(tmp_path / "var" / "outbox.log")
    assert phone.latest_code("c1") is None and phone.latest_any() is None
    phone.send("c1", "111111", "approve")
    phone.send("c2", "222222", "approve")
    phone.send("c1", "333333", "approve")
    assert (
        phone.latest_code("c1") == "333333" and phone.latest_code("c2") == "222222" and phone.latest_any() == "333333"
    )
    assert stat.S_IMODE(Path(tmp_path / "var" / "outbox.log").stat().st_mode) == 0o600
    assert json.loads((tmp_path / "var" / "outbox.log").read_text().splitlines()[0])["customer_id"] == "c1"
```

The file phone returns `None` before anything is sent; after three messages, the newest for `c1` is `333333`, for `c2` is `222222`, and the newest overall is `333333`; the file permission is `0o600`, and the first line is JSON with the customer id.

```python
def test_the_memory_phone_remembers_messages():
    phone = MemoryOtpChannel()
    assert phone.latest_code("c1") is None
    phone.send("c1", "123456", "approve")
    assert phone.latest_code("c1") == "123456" and phone.messages == [("c1", "123456", "approve")]
```

The in-memory phone returns `None` at first, then the code it was sent, and keeps the message tuple.

```python
def ok(request):
    return httpx.Response(200, json={"ok": True})
```

A tiny handler returning a 200 JSON answer (kept for the fault tests).

```python
def test_a_dropped_response_is_processed_by_the_server_and_lost_to_the_caller():
    seen = []

    def server(request):
        seen.append(request.url.path)
        return httpx.Response(201, json={"done": True})

    script = FaultScript()
    client = httpx.Client(transport=FaultInjectingTransport(httpx.MockTransport(server), script))
    script.push(Fault.DROP_RESPONSE)
    with pytest.raises(httpx.ReadTimeout):
        client.post("http://bank.test/v1/payments")
    assert seen == ["/v1/payments"]  # the bank got it
    assert client.post("http://bank.test/v1/payments").status_code == 201
```

A mock server records each request path. With `DROP_RESPONSE` scripted, the client's call raises `httpx.ReadTimeout`, yet the server **saw** the request; the next call works and returns 201. `httpx.MockTransport` answers requests with a Python function instead of a network.

```python
def test_a_dropped_request_never_reaches_the_server():
    seen = []
    script = FaultScript()

    def server(request):
        seen.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=FaultInjectingTransport(httpx.MockTransport(server), script))
    script.push(Fault.DROP_REQUEST, 2)
    for _ in range(2):
        with pytest.raises(httpx.ConnectTimeout):
            client.get("http://bank.test/")
    assert seen == [] and client.get("http://bank.test/").status_code == 200 and script.next() is None
    client.close()
```

Two `DROP_REQUEST` faults make two calls raise `httpx.ConnectTimeout` without the server seeing anything; the third call works, and the script is then empty (`next()` is `None`).

### `tests/unit/infrastructure/test_logging_retry.py` (82 lines)

```python
import io
import logging
import random

import pytest

from open_banking.application.logs import fields
from open_banking.infrastructure.logging import configure_logging, redact
from open_banking.infrastructure.retry import retry
```

Tests of log redaction, the logfmt formatter and the retry helper. The imports: `io`, `logging`, `random`, `pytest` and the project parts.

```python
@pytest.mark.parametrize(
    "text", ["4242424242424242", "4242 4242 4242 4242", "card 4242-4242-4242-4242 declined", "order 12345678901234567"]
)
```

Seven bad (name, value) pairs: port 0, port 70000, port `abc`, fee 10001, 11 retry attempts, level `LOUD`, pool size -3.

```python
def test_anything_shaped_like_a_card_number_is_masked(text):
    assert "4242" not in redact(text) and "[redacted-number]" in redact(text)
```

After `redact`, `4242` is gone and `[redacted-number]` appears.

```python
def test_ordinary_numbers_are_left_alone():
    assert redact("amount 4250 id 1234567 ts 2026-10-20T09:00:00") == "amount 4250 id 1234567 ts 2026-10-20T09:00:00"
```

Amounts, a 7-digit id and a timestamp are unchanged.

```python
def test_log_lines_are_logfmt_with_fields_and_withhold_secrets():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("t").info(
        "captured", extra=fields(payment_id="p1", card_token="tok_secret", note="a b", pan="4242424242424242")
    )
    line = stream.getvalue().strip()
    assert line.startswith("ts=") and "level=INFO" in line and "msg=captured" in line
    assert "payment_id=p1" in line and 'note="a b"' in line
    assert "card_token=[withheld]" in line and "tok_secret" not in line
    assert "4242424242424242" not in line
```

Logging into an in-memory stream: the line starts with `ts=`, has `level=INFO` and `msg=captured`, shows `payment_id=p1`, quotes a value containing a space (`note="a b"`), withholds the token (`card_token=[withheld]`, no `tok_secret`) and masks the card number.

```python
def test_exceptions_are_logged_on_one_line():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("t").exception("failed")
    assert len(stream.getvalue().strip().splitlines()) == 1 and "ValueError" in stream.getvalue()
```

A logged exception stays on one line and names `ValueError`.

```python
def test_retry_returns_the_first_success_without_sleeping():
    sleeps: list[float] = []
    assert retry(lambda: 7, attempts=3, base_delay_seconds=1, retry_on=(OSError,), sleep=sleeps.append) == 7
    assert sleeps == []
```

A function that works at once returns its value (7) and never sleeps.

```python
def test_retry_backs_off_exponentially_with_jitter_and_stops_at_the_last_attempt():
    calls: list[int] = []
    sleeps: list[float] = []
    notes: list[int] = []

    def flaky():
        calls.append(1)
        raise OSError("down")

    with pytest.raises(OSError, match="down"):
        retry(
            flaky,
            attempts=4,
            base_delay_seconds=1,
            retry_on=(OSError,),
            sleep=sleeps.append,
            rng=random.Random(1),
            on_retry=lambda attempt, error: notes.append(attempt),
        )
    assert len(calls) == 4 and notes == [1, 2, 3]
    assert [0.5 <= pause / (2**index) <= 1.0 for index, pause in enumerate(sleeps)] == [True, True, True]
```

A function that always fails with `OSError`. With 4 attempts it is called 4 times, `on_retry` hears attempts 1, 2, 3, and the three pauses fall between 0.5 and 1.0 times 1, 2 and 4 seconds (exponential growth with jitter). A seeded `random.Random(1)` makes the jitter repeatable. The last error propagates.

```python
def test_retry_does_not_catch_other_errors():
    with pytest.raises(KeyError):
        retry(lambda: {"a": 1}["x"], attempts=3, base_delay_seconds=0, retry_on=(OSError,))
```

A `KeyError` is not in `retry_on`, so it passes straight through.

```python
def test_retry_needs_at_least_one_attempt():
    with pytest.raises(ValueError, match="attempts"):
        retry(lambda: 1, attempts=0, base_delay_seconds=0, retry_on=(OSError,))
```

`attempts=0` raises `ValueError`.

### `tests/unit/interfaces/test_bank_api.py` (498 lines)

```python
"""The bank's HTTP API, end to end through FastAPI's test client over in-memory storage and real cryptography."""

from __future__ import annotations

import base64
import io
import json
import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from open_banking.application.merchant.checkout import BankUnavailableError, Paid, Refused
from open_banking.domain.errors import ClientAuthenticationError
from open_banking.domain.money import Money
from open_banking.infrastructure import keys
from open_banking.infrastructure.bank_client import HttpBankGateway
from open_banking.infrastructure.faults import Fault, FaultInjectingTransport, FaultScript
from open_banking.infrastructure.logging import configure_logging
from open_banking.interfaces.bank_api import create_bank_app
from tests.unit.conftest import BANK_URL, World, build_world
from tests.unit.helpers import dpop, sign_request
```

The bank's HTTP API, end to end through FastAPI's `TestClient` over in-memory storage and **real** cryptography. `from __future__ import annotations` makes type hints lazy text. New imports: `base64`, `io` (in-memory text streams), `logging`, `httpx`, and the merchant's real HTTP gateway and fault layer.

```python
@pytest.fixture
def stack() -> Iterator[tuple[TestClient, World]]:
    world = build_world()
    with TestClient(create_bank_app(lambda: world.bank), base_url=BANK_URL, raise_server_exceptions=False) as client:
        yield client, world
```

A fixture (the `@pytest.fixture` line above it) named `stack`. It builds the world and a `TestClient` around the real bank app, with `base_url=BANK_URL` so request URLs match what proofs are bound to, and `raise_server_exceptions=False` so a server error becomes a 500 answer instead of raising. `yield` hands the client and the world to the test; leaving the `with` block shuts the app down.

```python
def terms_json(maximum: int = 5000, **extra: Any) -> dict[str, Any]:
    return {
        "payer_account": "acct-ada",
        "payee_account": "acct-shop",
        "client_id": "merchant-1",
        "max_amount_minor": maximum,
        "currency": "USD",
        "scope": "single-payment",
        "period_seconds": None,
        **extra,
    }
```

A helper: the terms as the JSON body the API expects, with any field overridable (`**extra` merges extras).

```python
def consent_over_http(client: TestClient, world: World, maximum: int = 5000) -> dict[str, Any]:
    started = client.post(
        "/v1/sca/challenges", json={"username": "ada", "pin": world.ada_pin, "terms": terms_json(maximum)}
    )
    assert started.status_code == 201
    code = world.phone.latest_code(world.db.customers_by_name("ada").customer_id)
    proof = client.post(f"/v1/sca/challenges/{started.json()['challenge_id']}/verify", json={"code": code})
    assert proof.status_code == 200 and proof.headers["Cache-Control"] == "no-store"
    created = client.post(
        "/v1/consents",
        json={"terms": terms_json(maximum)},
        headers={"Authorization": f"SCA {proof.json()['authentication_proof']}"},
    )
    assert created.status_code == 201
    body: dict[str, Any] = created.json()
    return body
```

Does the whole customer side over HTTP: start the challenge (201), read the code from the phone, verify it (200, and the answer must carry `Cache-Control: no-store`), then create the consent with the header `Authorization: SCA <proof>` (201), and return the consent JSON.

```python
def token_over_http(client: TestClient, world: World) -> str:
    merchant = world.merchant
    response = client.post(
        "/oauth/token",
        data={"grant_type": "client_credentials"},
        auth=(merchant.client_id, merchant.secret),
        headers={"DPoP": dpop(merchant, world, "POST", "/oauth/token")},
    )
    assert response.status_code == 200 and response.json()["token_type"] == "DPoP"
    return str(response.json()["access_token"])
```

The merchant gets a token: a form post of `grant_type=client_credentials` with HTTP Basic credentials and a DPoP proof for the token URL. It requires status 200 and token type `DPoP`.

```python
def pay_over_http(client, world, token, consent_id, amount=3000, key="k-1", merchant=None, **overrides):
    merchant = merchant or world.merchant
    signature = sign_request(merchant, consent_id, "acct-shop", Money(amount, "USD"), key)
    headers = {
        "Authorization": f"DPoP {token}",
        "DPoP": dpop(merchant, world, "POST", "/v1/payments", token),
        "Idempotency-Key": key,
        "X-Request-Signature": signature,
    }
    headers.update(overrides)
    body = {"consent_id": consent_id, "payee_account": "acct-shop", "amount_minor": amount, "currency": "USD"}
    return client.post("/v1/payments", json=body, headers={k: v for k, v in headers.items() if v is not None})
```

A signed, proof-carrying payment request. It builds the signature and the four headers; `headers.update(overrides)` lets a test replace a header, and a header set to `None` is dropped (the dictionary comprehension at the end keeps only headers whose value is not `None`).

```python
def test_the_whole_lesson_flow_over_http(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    assert consent["scope"] == "single-payment" and consent["revoked_at"] is None
    token = token_over_http(client, world)
    paid = pay_over_http(client, world, token, consent["consent_id"])
    assert paid.status_code == 201 and paid.json()["status"] == "POSTED" and "Idempotent-Replay" not in paid.headers
    replay = pay_over_http(client, world, token, consent["consent_id"])
    assert replay.status_code == 201 and replay.json() == paid.json() and replay.headers["Idempotent-Replay"] == "true"
    assert world.db.balance("acct-ada") == 47_000 and world.db.ledger_sum() == 0
    fetched = client.get(
        f"/v1/payments/{paid.json()['payment_id']}",
        headers={
            "Authorization": f"DPoP {token}",
            "DPoP": dpop(world.merchant, world, "GET", f"/v1/payments/{paid.json()['payment_id']}", token),
        },
    )
    assert fetched.status_code == 200 and fetched.json() == paid.json()
```

Consent over HTTP, a token, a payment (201, `POSTED`, no replay header), the same payment again (201, identical body, `Idempotent-Replay: true`), balances (Ada 470.00, ledger sum 0), and then the merchant reads the payment back with a proof for that `GET`.

```python
def test_the_banks_signature_on_a_result_verifies_against_its_published_key(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    paid = pay_over_http(client, world, token_over_http(client, world), consent["consent_id"])
    [bank_key] = client.get("/.well-known/bank-keys.json").json()["keys"]
    message = json.dumps(paid.json(), sort_keys=True, separators=(",", ":")).encode()
    assert keys.verify(bank_key, message, paid.headers["Bank-Signature"])
    assert not keys.verify(bank_key, message + b" ", paid.headers["Bank-Signature"])
```

The bank publishes its key at `/.well-known/bank-keys.json`; the `Bank-Signature` header on the payment verifies against the canonical JSON of the body and fails if the message is changed by one byte.

```python
def test_a_payment_without_a_credential_or_with_a_bad_one_is_a_401(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    token = token_over_http(client, world)
    for overrides in (
        {"Authorization": None},
        {"DPoP": None},
        {"Authorization": "DPoP nonsense"},
        {"Authorization": "Bearer " + token},
    ):
        response = pay_over_http(
            client, world, token, consent["consent_id"], key=f"k-{len(overrides)}-{id(overrides)}", **overrides
        )
        assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_client_credential"
        assert response.headers["WWW-Authenticate"].startswith("DPoP")
    assert world.db.payments == {}
```

Four bad credentials (no `Authorization`, no `DPoP`, a made-up token, the wrong scheme `Bearer`): each is 401 with the code `invalid_client_credential` and a `WWW-Authenticate` header starting with `DPoP`; no payment exists. The `key=` argument uses `id(overrides)` to be different each time.

```python
def test_a_stolen_token_with_another_key_is_a_401(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    token = token_over_http(client, world)
    response = pay_over_http(
        client,
        world,
        token,
        consent["consent_id"],
        DPoP=dpop(world.other_merchant, world, "POST", "/v1/payments", token),
    )
    assert response.status_code == 401
```

A right token with a proof from the other merchant's key is 401.

```python
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "amount_exceeds_consent"),
        (404, "consent_not_found"),
        (400, "request_not_as_signed"),
        (422, "insufficient_funds"),
    ],
)
```

Four refusals and the HTTP status each must have: amount over the consent 403, unknown consent 404, request not as signed 400 and insufficient funds 422.

```python
def test_refusals_map_to_statuses_and_keep_their_own_code(stack, status, code):
    client, world = stack
    token = token_over_http(client, world)
    consent = consent_over_http(client, world, maximum=90_000 if code == "insufficient_funds" else 5000)
    if code == "amount_exceeds_consent":
        response = pay_over_http(client, world, token, consent["consent_id"], amount=6000)
    elif code == "consent_not_found":
        response = pay_over_http(client, world, token, "00000000-0000-4000-8000-0000000000ff")
    elif code == "request_not_as_signed":
        response = pay_over_http(
            client,
            world,
            token,
            consent["consent_id"],
            **{"X-Request-Signature": keys.sign(world.merchant.private_pem, b"other")},
        )
    else:
        response = pay_over_http(client, world, token, consent["consent_id"], amount=60_000)
    assert response.status_code == status and response.json()["error"]["code"] == code
```

For each case the test provokes that refusal (a payment of 60.00 against 50.00; a made-up consent id; a wrong signature; a 600.00 payment from a 500.00 account under a 900.00 consent) and requires the status and the error code.

```python
def test_a_missing_idempotency_key_and_a_reused_one_are_refused(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    token = token_over_http(client, world)
    missing = pay_over_http(client, world, token, consent["consent_id"], **{"Idempotency-Key": None})
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "invalid_idempotency_key"
    pay_over_http(client, world, token, consent["consent_id"], amount=3000, key="k-same")
    reused = pay_over_http(client, world, token, consent["consent_id"], amount=3100, key="k-same")
    assert reused.status_code == 422 and reused.json()["error"]["code"] == "idempotency_key_reused"
```

No `Idempotency-Key` header: 400 `invalid_idempotency_key`. The same key with a different amount: 422 `idempotency_key_reused`.

```python
def test_validation_errors_are_400_and_never_echo_the_pin_or_the_code(stack):
    client, world = stack
    bad_bodies = [
        {"username": "ada", "pin": "9999", "terms": {**terms_json(), "max_amount_minor": 50.5}},
        {"username": "ada", "pin": "9999", "terms": {**terms_json(), "currency": "usd"}},
        {"username": "ada", "pin": "9999", "terms": {**terms_json(), "scope": "forever"}},
        {"username": "ada", "pin": "9999", "terms": terms_json(), "extra": 1},
    ]
    for body in bad_bodies:
        response = client.post("/v1/sca/challenges", json=body)
        assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
        assert "9999" not in response.text
    started = client.post("/v1/sca/challenges", json={"username": "ada", "pin": world.ada_pin, "terms": terms_json()})
    bad_code = client.post(f"/v1/sca/challenges/{started.json()['challenge_id']}/verify", json={"code": "12ab56"})
    assert bad_code.status_code == 400 and "12ab56" not in bad_code.text
```

Four invalid challenge bodies (a fractional amount, a lower-case currency, an unknown scope, an extra field) all give 400 `invalid_request` and the response text never contains the PIN. A code that is not six digits (`12ab56`) gives 400 and is not echoed.

```python
def test_wrong_and_locking_codes_over_http(stack):
    client, world = stack
    started = client.post(
        "/v1/sca/challenges", json={"username": "ada", "pin": world.ada_pin, "terms": terms_json()}
    ).json()
    url = f"/v1/sca/challenges/{started['challenge_id']}/verify"
    answers = [client.post(url, json={"code": "000000"}) for _ in range(3)]
    assert [a.json()["error"]["code"] for a in answers] == ["wrong_code", "wrong_code", "challenge_locked"]
    assert answers[0].json()["error"]["attempts_left"] == 2 and all(a.status_code == 401 for a in answers)
    assert client.post("/v1/sca/challenges/not-a-uuid/verify", json={"code": "000000"}).status_code == 404
```

Three wrong codes give 401 with `wrong_code`, `wrong_code`, `challenge_locked`, and the first reports 2 attempts left. A challenge id that is not a UUID is a 404.

```python
def test_a_consent_without_a_proof_scheme_is_refused_and_a_wrong_pin_is_a_401(stack):
    client, world = stack
    assert client.post("/v1/consents", json={"terms": terms_json()}).status_code == 401
    assert (
        client.post("/v1/consents", json={"terms": terms_json()}, headers={"Authorization": "Bearer x"}).status_code
        == 401
    )
    wrong_pin = client.post("/v1/sca/challenges", json={"username": "ada", "pin": "000000", "terms": terms_json()})
    assert wrong_pin.status_code == 401 and wrong_pin.json()["error"]["code"] == "authentication_failed"
    not_mine = client.post(
        "/v1/sca/challenges",
        json={"username": "ada", "pin": world.ada_pin, "terms": terms_json(payer_account="acct-bob")},
    )
    assert not_mine.status_code == 403 and not_mine.json()["error"]["code"] == "account_not_owned"
    unknown = client.post(
        "/v1/sca/challenges", json={"username": "ada", "pin": world.ada_pin, "terms": terms_json(client_id="nobody")}
    )
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "unknown_client"
```

No authorization, or `Bearer x`, for a consent: 401. A wrong PIN: 401 `authentication_failed`. A payer account that is not Ada's: 403 `account_not_owned`. An unknown merchant: 422 `unknown_client`.

```python
def test_revocation_over_http(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    wrong = client.post(f"/v1/consents/{consent['consent_id']}/revoke", json={"username": "ada", "pin": "000000"})
    assert wrong.status_code == 401
    revoked = client.post(
        f"/v1/consents/{consent['consent_id']}/revoke", json={"username": "ada", "pin": world.ada_pin}
    )
    assert revoked.status_code == 200 and revoked.json()["revoked_at"] is not None
    token = token_over_http(client, world)
    refused = pay_over_http(client, world, token, consent["consent_id"])
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "consent_revoked"
    missing = client.post("/v1/consents/not-a-uuid/revoke", json={"username": "ada", "pin": world.ada_pin})
    assert missing.status_code == 404
```

A wrong PIN cannot revoke (401); the right PIN does (200, with a revocation time); a payment under the revoked consent is 403 `consent_revoked`; an id that is not a UUID is 404.

```python
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic !!!"},
        {"Authorization": "Bearer abc"},
        {"Authorization": "Basic " + base64.b64encode(b"merchant-1:wrong").decode()},
    ],
)
```

Four bad token-endpoint credentials: none, invalid base64, the wrong scheme, a wrong secret.

```python
def test_the_token_endpoint_refuses_bad_credentials(stack, headers):
    client, world = stack
    proof = dpop(world.merchant, world, "POST", "/oauth/token")
    response = client.post(
        "/oauth/token", data={"grant_type": "client_credentials"}, headers={**headers, "DPoP": proof}
    )
    assert response.status_code == 401
```

Each gives 401.

```python
def test_the_token_endpoint_needs_the_client_credentials_grant_and_a_proof(stack):
    client, world = stack
    auth = (world.merchant.client_id, world.merchant.secret)
    proof = dpop(world.merchant, world, "POST", "/oauth/token")
    assert (
        client.post("/oauth/token", data={"grant_type": "password"}, auth=auth, headers={"DPoP": proof}).status_code
        == 401
    )
    assert client.post("/oauth/token", data={"grant_type": "client_credentials"}, auth=auth).status_code == 401
```

A different grant type (`password`) is 401; so is the right grant without a DPoP proof.

```python
def test_health_live_and_a_down_dependency(stack):
    import dataclasses

    from open_banking.application.bank.queries import CheckHealth

    client, world = stack
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health").json() == {"status": "ok", "checks": {}}

    class Down:
        def check(self) -> None:
            raise ConnectionError("database unreachable")

    down = dataclasses.replace(world.bank, health=CheckHealth({"database": Down()}))
    with TestClient(create_bank_app(lambda: down), base_url=BANK_URL) as down_client:
        response = down_client.get("/health")
    assert response.status_code == 503 and response.json()["checks"] == {"database": "ConnectionError"}
```

`/health/live` is ok; `/health` with no probes is ok with empty checks; with a failing probe (the container is copied with `dataclasses.replace` and its health use case replaced) it is 503 and names only the error class.

```python
def test_an_unexpected_error_is_a_generic_500_and_the_container_closes_on_shutdown(stack):
    import dataclasses

    _, world = stack
    closed: list[bool] = []

    class Boom:
        def execute(self, *args):
            raise RuntimeError("secret internals")

    broken = dataclasses.replace(world.bank, get_payment=Boom(), close=lambda: closed.append(True))
    with TestClient(create_bank_app(lambda: broken), base_url=BANK_URL, raise_server_exceptions=False) as client:
        token = token_over_http(client, world)
        response = client.get(
            "/v1/payments/x",
            headers={
                "Authorization": f"DPoP {token}",
                "DPoP": dpop(world.merchant, world, "GET", "/v1/payments/x", token),
            },
        )
        assert response.status_code == 500 and "secret" not in response.text
    assert closed == [True]
```

The `get_payment` use case is replaced by an object whose `execute` raises `RuntimeError("secret internals")`. The answer is 500 and its text never contains `secret`; when the app shuts down the container's `close` was called once.

```python
def test_logs_never_contain_the_pin_the_code_the_secret_or_a_token(stack):
    client, world = stack
    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    try:
        consent = consent_over_http(client, world)
        token = token_over_http(client, world)
        pay_over_http(client, world, token, consent["consent_id"])
        client.post("/v1/sca/challenges", json={"username": "ada", "pin": "000000", "terms": terms_json()})
    finally:
        logging.getLogger().handlers.clear()
    code = world.phone.latest_code(world.db.customers_by_name("ada").customer_id)
    output = stream.getvalue()
    assert "payment posted" in output and "strong authentication succeeded" in output
    for secret in (world.ada_pin, code, world.merchant.secret, token, "000000"):
        assert secret not in output
```

Logging is switched to `DEBUG` writing into an in-memory stream. The test does a full consent and payment and a failed login, then requires that the log contains `payment posted` and `strong authentication succeeded` but not the PIN, the real code, the merchant's secret, the access token or the wrong PIN. The `finally` block removes the log handlers.

```python
class Bridge(httpx.BaseTransport):
    """Lets the merchant's real HTTP client talk to the bank app inside the test process."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        sent = self._client.send(request)
        return httpx.Response(sent.status_code, headers=sent.headers, content=sent.content)
```

Lets the merchant's real HTTP client talk to the bank app inside the test process: it sends each request through the `TestClient` and copies the answer into an `httpx.Response`.

```python
def gateway(
    client: TestClient, world: World, script: FaultScript | None = None, attempts: int = 3, verify_results: bool = True
):
    transport = FaultInjectingTransport(Bridge(client), script or FaultScript())
    sleeps: list[float] = []
    instance = HttpBankGateway(
        httpx.Client(transport=transport),
        bank_url=BANK_URL,
        client_id=world.merchant.client_id,
        client_secret=world.merchant.secret,
        private_pem=world.merchant.private_pem,
        attempts=attempts,
        retry_base_delay_seconds=0.01,
        clock=world.clock.now,
        sleep=sleeps.append,
        bank_public_jwk=world.bank.signing_jwk if verify_results else None,
    )
    return instance, sleeps
```

Builds the merchant's real `HttpBankGateway` for the test: its transport is the `Bridge` wrapped in the fault layer, the clock is the test clock, `sleep` just records the pauses in a list, and the bank's public key is passed unless `verify_results` is false.

```python
def test_the_merchants_client_pays_and_checks_the_banks_signature(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    merchant, _ = gateway(client, world)
    result = merchant.initiate_payment(
        consent_id=consent["consent_id"], payee_account="acct-shop", amount=Money(3000, "USD"), idempotency_key="g-1"
    )
    assert isinstance(result, Paid) and world.db.balance("acct-shop") == 3000
```

A real client payment returns `Paid`, and the shop is credited 30.00.

```python
def test_the_merchants_client_reports_the_banks_refusal(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    merchant, _ = gateway(client, world)
    result = merchant.initiate_payment(
        consent_id=consent["consent_id"], payee_account="acct-shop", amount=Money(6000, "USD"), idempotency_key="g-1"
    )
    assert result == Refused("amount_exceeds_consent", "amount 60.00 exceeds consent max 50.00")
```

Asking for 60.00 under a 50.00 consent returns `Refused` with the bank's code and message.

```python
def warm_up(merchant, client, world) -> None:
    """One ordinary payment, so the merchant's token is cached and scripted faults hit payment requests only."""
    consent = consent_over_http(client, world)
    merchant.initiate_payment(
        consent_id=consent["consent_id"],
        payee_account="acct-shop",
        amount=Money(1000, "USD"),
        idempotency_key="warm-up",
    )
```

One ordinary payment so the client's token is cached and later scripted faults hit payment requests only.

```python
def test_a_lost_response_is_retried_with_the_same_key_and_pays_once(stack):
    client, world = stack
    script = FaultScript()
    merchant, sleeps = gateway(client, world, script)
    warm_up(merchant, client, world)
    consent = consent_over_http(client, world)
    script.push(Fault.DROP_RESPONSE)
    result = merchant.initiate_payment(
        consent_id=consent["consent_id"], payee_account="acct-shop", amount=Money(3000, "USD"), idempotency_key="g-1"
    )
    assert isinstance(result, Paid) and len(world.db.payments) == 2
    assert world.db.balance("acct-ada") == 50_000 - 1000 - 3000 and len(sleeps) == 1
```

With `DROP_RESPONSE` scripted once, the bank posts the payment but the answer is lost; the client retries with the same key and gets the stored answer. Result: `Paid`, exactly two payments exist (the warm-up and this one), Ada paid 10.00 + 30.00, and one pause was recorded.

```python
def test_when_every_answer_is_lost_the_merchant_gives_up_but_the_bank_paid_once(stack):
    client, world = stack
    script = FaultScript()
    merchant, _ = gateway(client, world, script)
    warm_up(merchant, client, world)
    consent = consent_over_http(client, world)
    script.push(Fault.DROP_RESPONSE, 3)
    with pytest.raises(BankUnavailableError, match="3 attempts"):
        merchant.initiate_payment(
            consent_id=consent["consent_id"],
            payee_account="acct-shop",
            amount=Money(3000, "USD"),
            idempotency_key="g-1",
        )
    assert len(world.db.payments) == 2 and world.db.balance("acct-ada") == 50_000 - 1000 - 3000
    again = merchant.initiate_payment(
        consent_id=consent["consent_id"], payee_account="acct-shop", amount=Money(3000, "USD"), idempotency_key="g-1"
    )
    assert isinstance(again, Paid) and len(world.db.payments) == 2  # the later retry finds the stored answer
```

With three dropped answers the client raises `BankUnavailableError` mentioning `3 attempts`, yet the bank paid once; asking again later with the same key finds the stored answer: still two payments.

```python
def test_a_request_that_never_reaches_the_bank_is_retried_too(stack):
    client, world = stack
    script = FaultScript()
    merchant, _ = gateway(client, world, script)
    warm_up(merchant, client, world)
    consent = consent_over_http(client, world)
    script.push(Fault.DROP_REQUEST, 2)
    result = merchant.initiate_payment(
        consent_id=consent["consent_id"], payee_account="acct-shop", amount=Money(3000, "USD"), idempotency_key="g-1"
    )
    assert isinstance(result, Paid) and len(world.db.payments) == 2
```

With two dropped requests the client retries and the third attempt pays.

```python
def test_the_merchant_refuses_a_result_the_bank_did_not_sign(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    merchant, _ = gateway(client, world)
    merchant._bank_public_jwk = keys.generate_keypair()[1]  # a different bank key than the one that signs
    with pytest.raises(ClientAuthenticationError, match="not signed by the bank"):
        merchant.initiate_payment(
            consent_id=consent["consent_id"],
            payee_account="acct-shop",
            amount=Money(3000, "USD"),
            idempotency_key="g-1",
        )
```

If the merchant is configured with a different bank key than the one that signs, a successful payment answer is refused with `not signed by the bank`.

```python
def test_the_merchants_client_fetches_a_new_token_when_the_old_one_expires(stack):
    client, world = stack
    merchant, _ = gateway(client, world)
    for index in range(2):
        consent = consent_over_http(client, world)
        result = merchant.initiate_payment(
            consent_id=consent["consent_id"],
            payee_account="acct-shop",
            amount=Money(1000, "USD"),
            idempotency_key=f"g-{index}",
        )
        assert isinstance(result, Paid)
        world.clock.advance(timedelta(minutes=10))
    assert len(world.db.tokens) == 2
```

Two payments ten minutes apart use two tokens (the first expired), so the token table has two rows.

```python
def test_a_wrong_secret_stops_the_merchants_client_at_once(stack):
    client, world = stack
    merchant, _ = gateway(client, world)
    merchant._client_secret = "wrong"
    with pytest.raises(ClientAuthenticationError):
        merchant.initiate_payment(
            consent_id="c", payee_account="acct-shop", amount=Money(1, "USD"), idempotency_key="g-1"
        )
```

A wrong client secret raises `ClientAuthenticationError` immediately instead of being retried.

### `tests/unit/interfaces/test_merchant_api_and_clis.py` (182 lines)

```python
"""The merchant's HTTP API with a scripted bank, the command-line programs' pure parts, and its fault layer."""

from __future__ import annotations

import argparse
import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from open_banking.application.merchant.checkout import (
    BankUnavailableError,
    CompleteCheckout,
    CreateCheckout,
    InMemoryCheckouts,
    MerchantProfile,
    Paid,
    Refused,
)
from open_banking.infrastructure import keys
from open_banking.infrastructure.faults import FaultScript
from open_banking.interfaces import admin_cli, customer_cli
from open_banking.interfaces.merchant_api import create_merchant_app
from open_banking.interfaces.merchant_container import MerchantContainer
from tests.unit.conftest import World, build_world
from tests.unit.fakes import SequentialIds
```

The merchant's HTTP API with a scripted bank, the pure parts of the admin and customer programs, and the fault layer. `argparse` makes argument objects; `dataclasses` copies containers with one field replaced; `json` is used once.

```python
class Gateway:
    def __init__(self, *answers):
        self.answers = list(answers)

    def initiate_payment(self, *, consent_id, payee_account, amount, idempotency_key):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
```

A fake bank gateway: a list of answers, one returned (or raised, if it is an exception) per call.

```python
def container(*answers) -> MerchantContainer:
    checkouts = InMemoryCheckouts()
    return MerchantContainer(
        create_checkout=CreateCheckout(checkouts, SequentialIds()),
        complete_checkout=CompleteCheckout(checkouts, Gateway(*answers), MerchantProfile("merchant-1", "acct-shop")),
        checkouts=checkouts,
        faults=FaultScript(),
        client_id="merchant-1",
        close=lambda: None,
    )
```

Builds a merchant container around the fake gateway with in-memory checkouts, sequential ids, an empty fault script and a `close` that does nothing.

```python
@pytest.fixture
def merchant() -> Iterator[TestClient]:
    with TestClient(
        create_merchant_app(lambda: container(Paid("p-1")), "acct-shop"), raise_server_exceptions=False
    ) as client:
        yield client
```

A fixture giving a test client for a merchant app whose bank pays once (`Paid("p-1")`).

```python
def test_a_checkout_names_the_terms_the_customer_must_approve_at_the_bank(merchant):
    created = merchant.post("/v1/checkouts", json={"amount_minor": 3000, "currency": "USD"})
    body = created.json()
    assert created.status_code == 201 and body["status"] == "AWAITING_CONSENT" and body["amount"] == "30.00"
    assert body["consent_request"] == {
        "client_id": "merchant-1",
        "payee_account": "acct-shop",
        "max_amount_minor": 3000,
        "currency": "USD",
        "scope": "single-payment",
    }
    assert merchant.get(f"/v1/checkouts/{body['checkout_id']}").json() == body
```

Creating a checkout returns 201, status `AWAITING_CONSENT`, amount `30.00` and the `consent_request` (the merchant's id, payee account, maximum, currency and scope); reading it back gives the same JSON.

```python
def test_completing_a_checkout_with_a_consent_pays_it(merchant):
    checkout = merchant.post("/v1/checkouts", json={"amount_minor": 3000, "currency": "USD"}).json()
    done = merchant.post(f"/v1/checkouts/{checkout['checkout_id']}/complete", json={"consent_id": "c-1"})
    assert done.status_code == 200 and done.json()["status"] == "PAID" and done.json()["payment_id"] == "p-1"
```

Completing returns 200, `PAID` and the payment id `p-1`.

```python
def test_a_refusal_by_the_bank_is_a_403_with_its_reason():
    app = create_merchant_app(lambda: container(Refused("consent_expired", "consent expired")), "acct-shop")
    with TestClient(app) as client:
        checkout = client.post("/v1/checkouts", json={"amount_minor": 3000, "currency": "USD"}).json()
        done = client.post(f"/v1/checkouts/{checkout['checkout_id']}/complete", json={"consent_id": "c-1"})
    assert done.status_code == 403 and done.json()["refusal"] == {
        "code": "consent_expired",
        "message": "consent expired",
    }
```

A scripted refusal gives 403 and the refusal's code and message in the body.

```python
def test_a_bank_that_cannot_be_reached_is_a_retryable_504():
    app = create_merchant_app(lambda: container(BankUnavailableError("no answer after 3 attempts")), "acct-shop")
    with TestClient(app) as client:
        checkout = client.post("/v1/checkouts", json={"amount_minor": 3000, "currency": "USD"}).json()
        done = client.post(f"/v1/checkouts/{checkout['checkout_id']}/complete", json={"consent_id": "c-1"})
        assert done.status_code == 504 and done.json()["error"] == {
            "code": "bank_unavailable",
            "message": "no answer after 3 attempts",
            "retryable": True,
        }
        assert client.get(f"/v1/checkouts/{checkout['checkout_id']}").json()["status"] == "UNKNOWN"
```

A `BankUnavailableError` gives 504 with the error object including `retryable: true`; reading the checkout afterwards shows the status `UNKNOWN`.

```python
def test_validation_unknown_checkouts_health_and_unexpected_errors(merchant):
    assert merchant.post("/v1/checkouts", json={"amount_minor": 30.5, "currency": "USD"}).status_code == 400
    assert merchant.post("/v1/checkouts", json={"amount_minor": 0, "currency": "USD"}).status_code == 400
    assert merchant.get("/v1/checkouts/nope").json()["error"]["code"] == "checkout_not_found"
    assert merchant.post("/v1/checkouts/nope/complete", json={"consent_id": "c-1"}).status_code == 404
    assert merchant.get("/health").json() == {"status": "ok"} and merchant.get("/health/live").json() == {
        "status": "ok"
    }
```

A fractional amount and a zero amount give 400; an unknown checkout gives 404 `checkout_not_found` (for reading and for completing); both health endpoints answer ok.

```python
def test_the_container_is_closed_on_shutdown_and_unexpected_errors_are_generic():
    closed: list[bool] = []

    class Boom:
        def execute(self, *args):
            raise RuntimeError("secret internals")

    broken = dataclasses.replace(container(), create_checkout=Boom(), close=lambda: closed.append(True))  # type: ignore[arg-type]
    with TestClient(create_merchant_app(lambda: broken, "acct-shop"), raise_server_exceptions=False) as client:
        response = client.post("/v1/checkouts", json={"amount_minor": 3000, "currency": "USD"})
        assert response.status_code == 500 and "secret" not in response.text
    assert closed == [True]
```

A use case replaced by one that raises gives a 500 whose text has no `secret`; the container's `close` ran once at shutdown. `# type: ignore[arg-type]` silences the type checker for this deliberate substitution.

```python
def test_registering_a_merchant_and_enrolling_a_customer_write_secrets_only_to_private_files(tmp_path: Path):
    world: World = build_world()
    files = admin_cli.register_merchant(world.bank, "merchant-3", "Third Shop", tmp_path)
    key_file, env_file = files
    assert key_file.stat().st_mode & 0o777 == 0o600 and env_file.stat().st_mode & 0o777 == 0o600
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert values["MERCHANT_CLIENT_ID"] == "merchant-3" and len(values["MERCHANT_CLIENT_SECRET"]) >= 40
    assert world.db.clients["merchant-3"].public_jwk == keys.public_jwk_of(key_file.read_text())
    assert values["MERCHANT_CLIENT_SECRET"] not in world.db.clients["merchant-3"].secret_hash
    pin_file = admin_cli.enroll_customer(world.bank, "carol", "acct-bob", tmp_path)
    pin = pin_file.read_text().strip()
    assert pin_file.stat().st_mode & 0o777 == 0o600 and len(pin) == 6 and pin.isdigit()
    assert pin not in world.db.customers_by_name("carol").pin_hash
```

The admin functions create the key file and the `.env` file with permission `0o600` (`st_mode & 0o777` keeps the permission bits); the `.env` holds the client id and a secret of at least 40 characters; the bank has the matching public key and only a **hash** of the secret; the PIN file is private, holds six digits, and the PIN is not in the stored hash.

```python
def test_the_customer_program_builds_the_terms_the_bank_will_see():
    args = argparse.Namespace(
        max="50.00", payee="acct-shop", merchant="merchant-1", currency="USD", scope="single-payment", period_days="30"
    )
    assert customer_cli._terms(args, "acct-ada") == {
        "payer_account": "acct-ada",
        "payee_account": "acct-shop",
        "client_id": "merchant-1",
        "max_amount_minor": 5000,
        "currency": "USD",
        "scope": "single-payment",
        "period_seconds": None,
    }
    recurring = argparse.Namespace(**{**vars(args), "scope": "recurring", "period_days": "7"})
    assert customer_cli._terms(recurring, "acct-ada")["period_seconds"] == 7 * 86400
    with pytest.raises(customer_cli.CustomerError, match="--max"):
        customer_cli._terms(argparse.Namespace(**{**vars(args), "max": "lots"}), "acct-ada")
```

From the command-line values `50.00`, the payee, merchant and scope, the program builds exactly the terms dictionary (5000 minor units, no period). A recurring scope with 7 days gives 7 * 86400 seconds; a maximum such as `lots` raises `CustomerError` mentioning `--max`.

```python
def test_the_customer_program_reports_what_the_bank_said(capsys):
    import httpx

    with pytest.raises(customer_cli.CustomerError, match="authentication_failed: the username or PIN is not right"):
        customer_cli._check(
            httpx.Response(
                401, json={"error": {"code": "authentication_failed", "message": "the username or PIN is not right"}}
            )
        )
    assert customer_cli._check(httpx.Response(200, json={"ok": 1})) == {"ok": 1}
    parser = customer_cli._parser()
    assert (
        parser.parse_args(
            ["approve", "--username", "ada", "--account", "a", "--payee", "p", "--merchant", "m", "--max", "5.00"]
        ).scope
        == "single-payment"
    )
    assert json.dumps({}) == "{}" and capsys.readouterr().out == ""
```

`_check` turns a 401 answer into a `CustomerError` with the bank's code and message, and returns the JSON for a 200. The parser accepts the minimal `approve` arguments and defaults to the single-payment scope. `capsys` captures printed output, which is empty.

## Try it yourself

1. Run all unit tests:

   ```bash
   make test
   ```

   Expected: the unit tests pass and the coverage report shows the domain and application at or above 95 percent.

2. Run one file, verbosely:

   ```bash
   .venv/bin/python -m pytest tests/unit/application/test_payments.py -v
   ```

   Expected: each test name followed by `PASSED`.

3. Break something on purpose: in `src/open_banking/domain/consent.py` swap the order of two checks in `refusal_for`, run `make test`, read the failing assertion, then undo the change.

## Mistakes people make

- **Faking the cryptography.** A fake signer cannot show that a changed amount is detected; here the real signing runs.
- **Tests that depend on today's date.** The clock is fixed and moved by the test.
- **Asserting only that no exception happened.** Each test checks values: refusal codes, balances, ledger sums and stored rows.
- **Leaving a secret in a log.** One test captures the logs of a full scenario and searches them for the PIN, the code, the secret and a token.

## Check yourself

1. What is a fixture? *A function that prepares something a test needs; pytest passes it in by parameter name.*
2. Why is the scrypt cost tiny in tests? *So hashing is fast; the code path is the same.*
3. How does a test complete an authentication? *It reads the code that the in-memory phone received and submits it.*
4. What does the property test check after every step? *That the ledger sums to zero and the payer's balance is not negative.*
5. What does `monkeypatch` do? *Temporarily replaces an attribute, here to make a repository method fail.*

## Further reading

- pytest documentation: https://docs.pytest.org/en/stable/
- pytest fixtures: https://docs.pytest.org/en/stable/how-to/fixtures.html
- pytest parametrize: https://docs.pytest.org/en/stable/how-to/parametrize.html
- Hypothesis documentation: https://hypothesis.readthedocs.io/
- Python 3.12 `threading`: https://docs.python.org/3.12/library/threading.html
