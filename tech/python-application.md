# Python walkthrough: the application package, line by line

## What it is

This note reads every line of the `application` package: the bank's use cases (enroll a customer, register a merchant, start and verify a strong-authentication challenge, grant and revoke consent, issue an access token, authenticate a caller, initiate and read a payment, read balances, check health), the idempotent executor, the merchant's checkout, and the ports that describe what the use cases need from the outside world.

A **use case** is one thing a user can ask the service to do. It coordinates the domain objects and the outside world but contains no web or database code itself.

## Why this repository uses it

The lesson's guarantees are built here on top of the pure rules of the domain: a consent exists only after two factors for exactly the approved terms, a merchant's request is accepted only with a token bound to its key and a signature over the exact request, and a payment moves money at most once however often it is retried. Because the use cases reach the database, the one-time-code channel and the cryptography only through ports, the same code runs against PostgreSQL in production and in-memory fakes in the fast tests.

## Where it appears

| File | What it is |
| --- | --- |
| `src/open_banking/application/__init__.py` | A package marker with its docstring. |
| `src/open_banking/application/bank/__init__.py` | A package marker with its docstring. |
| `src/open_banking/application/merchant/__init__.py` | A package marker with its docstring. |
| `src/open_banking/application/logs.py` | The log field helper. |
| `src/open_banking/application/ports.py` | The clock and id ports both sides share. |
| `src/open_banking/application/bank/idempotent.py` | The idempotent executor. |
| `src/open_banking/application/bank/ports.py` | What the bank's use cases need from outside. |
| `src/open_banking/application/bank/queries.py` | Balances and the health check. |
| `src/open_banking/application/bank/enrollment.py` | Enroll a customer, register a merchant. |
| `src/open_banking/application/bank/access.py` | Issue and check DPoP-bound tokens. |
| `src/open_banking/application/bank/authentication.py` | PIN, then one-time code. |
| `src/open_banking/application/bank/consents.py` | Grant and revoke consent. |
| `src/open_banking/application/bank/payments.py` | Initiate and read a payment. |
| `src/open_banking/application/merchant/checkout.py` | The merchant's checkout. |

## How it works underneath

A use case's `execute` method opens a **transaction** through the unit-of-work factory, reads and locks the rows it needs, applies the domain's pure rules, writes the result and lets the transaction commit when the `with` block ends. If an exception escapes, everything rolls back. **Locks** make concurrent requests take turns: the idempotency key is locked per (merchant, key), the consent row and both account rows are locked with `FOR UPDATE`, in a fixed order. Secrets (PIN, merchant secret, one-time code, tokens) are never stored: only slow salted hashes or keyed digests. Comparisons of secret values run in constant time.

## Walkthrough of this repository's use



### `src/open_banking/application/__init__.py` (1 lines)

```python
"""Use cases of the bank and of the merchant, and the ports they need. Depends on the domain only."""
```

The docstring of the `application` package: the use cases of the bank and of the merchant, and the ports (descriptions of what they need from the outside world). It depends on the domain only.

### `src/open_banking/application/bank/__init__.py` (1 lines)

```python
"""The bank's use cases: strong authentication, consent, access tokens and payment initiation."""
```

The docstring of the `application.bank` package: the bank's use cases, which are strong authentication, consent, access tokens and payment initiation.

### `src/open_banking/application/merchant/__init__.py` (1 lines)

```python
"""The merchant's use cases: a checkout, and the payment it initiates with a consent the customer gave at the bank."""
```

The docstring of the `application.merchant` package: the merchant's use cases, a checkout and the payment it initiates with a consent the customer gave at the bank.

### `src/open_banking/application/logs.py` (10 lines)

```python
"""Structured-log helper shared by the use cases: `logger.info("msg", extra=fields(payment_id=...))`."""

from __future__ import annotations

from typing import Any


def fields(**values: Any) -> dict[str, Any]:
    """Attach key=value pairs to a log record. Never pass card data, tokens or secrets."""
    return {"fields": values}
```

`logs.py` is the whole helper behind every structured log line in the use cases. `from typing import Any` imports `Any`, a type hint meaning "a value of any type".

`def fields(**values: Any) -> dict[str, Any]:` has a parameter written `**values`. The two stars collect every **keyword argument** (`name=value` written at the call) into one dictionary called `values`. So `fields(payment_id="p1", amount="42.50")` receives `{"payment_id": "p1", "amount": "42.50"}`. The function returns `{"fields": values}`: the logging module will attach it to the log record under the name `fields`, and the formatter (see `logging-and-logfmt.md`) prints each pair as `key=value`. The docstring warns callers never to pass card data, tokens or secrets.

### `src/open_banking/application/ports.py` (23 lines)

```python
"""The ports both sides share: the clock and the source of identifiers, injected so tests are deterministic."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID
```

The ports both sides share: the clock and the source of identifiers, injected so tests are deterministic. A **port** is a description of something the application needs, without saying how it is provided. `Protocol` (from `typing`) describes a shape: any class that has the listed methods satisfies it, with no inheritance needed, and mypy checks it.

```python
class Clock(Protocol):
    """The current time, injected so tests are deterministic."""

    def now(self) -> datetime:
        """A timezone-aware UTC time."""
        ...
```

`Clock` has one method, `now()`, returning a timezone-aware UTC time. The body `...` (the **Ellipsis**) means "no code here". Injecting the clock lets tests move time.

```python
class IdSource(Protocol):
    """New identifiers, injected for the same reason."""

    def new_id(self) -> UUID:
        """A fresh UUID."""
        ...
```

`IdSource` has one method, `new_id()`, returning a fresh `UUID`. Injecting it lets tests use predictable ids.

### `src/open_banking/application/bank/idempotent.py` (56 lines)

```python
"""The idempotent executor of the bank: one transaction holds the key, the effect and the stored response together."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from open_banking.application.bank.ports import Fresh, Replay, UnitOfWork, UnitOfWorkFactory
from open_banking.application.logs import fields
from open_banking.application.ports import Clock
from open_banking.domain.errors import IdempotencyKeyReusedError
from open_banking.domain.idempotency import IdempotencyKey, StoredResponse
```

The idempotent executor of the bank. The docstring states the central idea: **one transaction holds the key, the effect and the stored response together**. `logging` writes log lines; `Callable` is the type of a function value; `timedelta` is a length of time; `Fresh` and `Replay` are the possible claim results; `IdempotencyKeyReusedError` is the error for a reused key.

```python
logger = logging.getLogger(__name__)
```

A logger named after the module, so log lines can be traced to this file.

```python
@dataclass(frozen=True, slots=True)
class Execution:
    """The response to send, and whether it came from the store instead of fresh work."""

    response: StoredResponse
    replayed: bool
```

What the executor returns: the response to send and `replayed`, which is `True` when the response came from the store instead of fresh work.

```python
class IdempotentExecutor:
    """Runs a write at most once per (client, key), however many times and however concurrently it is sent."""

    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock, key_time_to_live: timedelta) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._time_to_live = key_time_to_live
```

The docstring: it runs a write **at most once per (client, key)**, however many times and however concurrently it is sent. The constructor keeps the transaction factory, the clock and how long a key stays valid. Keys belong to a client (a merchant), so two merchants may use the same key text.

```python
    def execute(
        self,
        *,
        client_id: str,
        key: IdempotencyKey,
        request_fingerprint: str,
        work: Callable[[UnitOfWork], StoredResponse],
    ) -> Execution:
        """Claim the key, run `work` if it is new, store the response, commit. An exception rolls everything back."""
        with self._uow_factory() as uow:
            now = self._clock.now()
            claim = uow.keys.claim(client_id, key, request_fingerprint, now)
            if isinstance(claim, Replay):
                logger.info("replayed", extra=fields(client_id=client_id, idempotency_key=key.value))
                return Execution(claim.response, replayed=True)
            if not isinstance(claim, Fresh):
                raise IdempotencyKeyReusedError(
                    f"the Idempotency-Key {key.value!r} was already used for a different request"
                )
            response = work(uow)
            uow.keys.store(client_id, key, request_fingerprint, response, now, now + self._time_to_live)
            return Execution(response, replayed=False)
```

The parameters after `*` must be named. `work` is a function that performs the real effect inside the transaction and returns the response. The body: open a transaction (`with self._uow_factory() as uow:`; it commits when the block ends normally and rolls back on an exception); read the time; **claim** the key (`uow.keys.claim`), which takes a lock on the (client, key) pair and says what to do. `Replay`: the same request was done before, so log `replayed` and return the stored response without doing anything again. Not `Fresh` (so it must be `Reused`): the key came with a different request, so raise `IdempotencyKeyReusedError` (`{key.value!r}` prints the key with quotes). `Fresh`: run `work(uow)`, store the response under the key with an expiry of `now + time_to_live`, and return it as not replayed. If `work` raises anything, the transaction rolls back, so neither the effect nor the key is saved.

### `src/open_banking/application/bank/ports.py` (386 lines)

```python
"""The bank's ports: what its use cases need from the outside world, as interfaces the adapters implement."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from open_banking.domain.authentication import Challenge
from open_banking.domain.consent import AuthenticationProof, Consent
from open_banking.domain.idempotency import IdempotencyKey, StoredResponse
from open_banking.domain.ledger import Transfer
from open_banking.domain.money import Money

Jwk = Mapping[str, Any]
```

The bank's ports: what its use cases need from the outside world, as interfaces the adapters implement. The imports include `Mapping` (a read-only dictionary type), `AbstractContextManager` (the type of something usable in a `with` block), `Any`, and the domain types. `Jwk = Mapping[str, Any]` names the type of a public key written as a JSON object.

```python
class Crypto(Protocol):
    """Every cryptographic operation the bank and the merchant need. The adapter wraps the libraries."""

    def hash_secret(self, secret: str) -> str:
        """A salted, slow hash for a PIN or a client secret."""
        ...

    def verify_secret(self, secret: str, stored: str) -> bool:
        """Compare in constant time. A malformed `stored` is simply False."""
        ...
```

`Crypto` lists every cryptographic operation the bank and the merchant need; the adapter wraps the libraries. A **Protocol** lists methods an object must have; the body `...` means "no code here". `hash_secret` makes a salted, slow hash for a PIN or a client secret; `verify_secret` compares in constant time and treats a malformed stored value as simply false.

```python
    def new_code(self) -> str:
        """A fresh six-digit one-time code."""
        ...

    def code_digest(self, challenge_id: str, code: str) -> str:
        """The digest stored instead of the code; keyed, and tied to the challenge."""
        ...

    def new_token(self) -> str:
        """A fresh opaque bearer value (an access token or a proof of authentication)."""
        ...

    def token_digest(self, token: str) -> str:
        """The digest under which a token is stored; the token itself is never stored."""
        ...
```

`new_code` makes a fresh six-digit one-time code; `code_digest` makes the digest stored instead of the code, keyed and tied to the challenge; `new_token` makes a fresh opaque bearer value (an access token or a proof of authentication); `token_digest` makes the digest under which a token is stored (the token itself is never stored).

```python
    def thumbprint(self, jwk: Jwk) -> str:
        """The RFC 7638 thumbprint of a public key."""
        ...

    def verify_signature(self, public_jwk: Jwk, message: bytes, signature: str) -> bool:
        """True when `signature` (base64url) is the key's signature over `message`."""
        ...
```

`thumbprint` returns the RFC 7638 thumbprint of a public key: a standard short fingerprint of a key. `verify_signature` is true when a base64url signature is the key's signature over the message bytes.

```python
    def verify_dpop_proof(
        self, proof: str, public_jwk: Jwk, *, method: str, url: str, now: datetime, access_token: str | None
    ) -> DpopClaims:
        """Check a proof of possession: signature, method, URL, freshness, and the access token's hash.

        Raises `ClientAuthenticationError` when any of them is wrong.
        """
        ...
```

Checks a proof of possession (DPoP): the signature, the HTTP method, the URL, the freshness and the hash of the access token. It returns the verified claims and raises `ClientAuthenticationError` when anything is wrong. The keyword-only parameters come after `*`.

```python
@dataclass(frozen=True, slots=True)
class DpopClaims:
    """The parts of a verified proof of possession the bank keeps: its id (to refuse a replay) and its time."""

    jti: str
    issued_at: datetime
```

The parts of a verified proof of possession the bank keeps: its unique id `jti` (to refuse a replay) and its issue time.

```python
class OtpChannel(Protocol):
    """Delivers a one-time code to the customer's device. The bank never logs the code."""

    def send(self, customer_id: str, code: str, purpose: str) -> None:
        """Hand the code to the channel."""
        ...
```

Delivers a one-time code to the customer's device. The docstring: the bank never logs the code.

```python
@dataclass(frozen=True, slots=True)
class Account:
    """A bank account with its balance, which the database keeps equal to the sum of its postings."""

    account_id: str
    owner: str
    balance: Money
    vault: bool = False
```

A bank account with its balance, which the database keeps equal to the sum of its postings. `vault` marks the special account that holds opening money.

```python
@dataclass(frozen=True, slots=True)
class Customer:
    """A customer who can authenticate: a username, a PIN (stored as a slow hash) and the account they own."""

    customer_id: str
    username: str
    account_id: str
    pin_hash: str
```

A customer who can authenticate: an id, a username, the account they own and the PIN stored as a slow hash.

```python
@dataclass(frozen=True, slots=True)
class OAuthClient:
    """A registered merchant: its secret (stored as a slow hash) and the public key its requests are checked with."""

    client_id: str
    name: str
    secret_hash: str
    public_jwk: Jwk
```

A registered merchant: its id, name, its secret stored as a slow hash and the public key its requests are checked with.

```python
@dataclass(frozen=True, slots=True)
class AccessToken:
    """An issued token as stored: the digest of the token, whose client, which key it is bound to, until when."""

    token_digest: str
    client_id: str
    key_thumbprint: str
    created_at: datetime
    expires_at: datetime
```

An issued token as stored: the digest of the token, whose client, which key it is bound to (a thumbprint) and when it was created and expires.

```python
@dataclass(frozen=True, slots=True)
class PaymentRecord:
    """A payment the bank made under a consent."""

    payment_id: str
    consent_id: str
    client_id: str
    payer_account: str
    payee_account: str
    amount: Money
    created_at: datetime
```

A payment the bank made under a consent: ids, the two accounts, the amount and the time.

```python
class AccountRepository(Protocol):
    """Accounts, locked while a payment moves money."""

    def get(self, account_id: str) -> Account | None:
        """Read an account."""
        ...

    def get_for_update(self, account_id: str) -> Account | None:
        """Read and lock the account until the unit of work ends."""
        ...
```

Accounts, locked while a payment moves money. `get_for_update` reads and locks the account until the unit of work ends.

```python
class CustomerRepository(Protocol):
    """Customers."""

    def by_username(self, username: str) -> Customer | None:
        """Find by username."""
        ...

    def by_id(self, customer_id: str) -> Customer | None:
        """Find by id."""
        ...

    def add(self, customer: Customer) -> None:
        """Enroll a customer."""
        ...
```

Customers: find by username, find by id, enroll.

```python
class ClientRepository(Protocol):
    """Registered merchants."""

    def get(self, client_id: str) -> OAuthClient | None:
        """Find a client."""
        ...

    def add(self, client: OAuthClient) -> None:
        """Register a client."""
        ...
```

Registered merchants: find and register.

```python
class ChallengeRepository(Protocol):
    """Authentication challenges."""

    def add(self, challenge: Challenge) -> None:
        """Store a new challenge."""
        ...

    def get_for_update(self, challenge_id: str) -> Challenge | None:
        """Read and lock a challenge."""
        ...

    def save(self, challenge: Challenge) -> None:
        """Write the attempts and status of a challenge."""
        ...
```

Authentication challenges: add, read-and-lock, and save the attempts and status.

```python
class ProofRepository(Protocol):
    """Proofs of strong authentication, stored by the digest of their bearer value."""

    def add(self, proof: AuthenticationProof) -> None:
        """Store a proof."""
        ...

    def get_for_update(self, proof_id: str) -> AuthenticationProof | None:
        """Read and lock a proof."""
        ...

    def mark_consumed(self, proof_id: str) -> None:
        """A proof is good for one consent."""
        ...
```

Proofs of strong authentication, stored by the digest of their bearer value: add, read-and-lock, and mark consumed (a proof is good for one consent).

```python
class ConsentRepository(Protocol):
    """Consents."""

    def add(self, consent: Consent) -> None:
        """Store a consent."""
        ...

    def get_for_update(self, consent_id: str) -> Consent | None:
        """Read and lock a consent, so a revocation and a payment under it run one after the other."""
        ...

    def save(self, consent: Consent) -> None:
        """Write the revocation and use times of a consent."""
        ...

    def count(self) -> int:
        """How many consents exist."""
        ...

    def spent_since(self, consent_id: str, since: datetime, currency: str) -> Money:
        """What payments under this consent moved at or after `since`."""
        ...
```

Consents: add, read-and-lock (so a revocation and a payment under it run one after the other), save, count, and `spent_since`, what payments under the consent moved at or after a moment.

```python
class TokenRepository(Protocol):
    """Access tokens and the proofs of possession already seen."""

    def add(self, token: AccessToken) -> None:
        """Store an issued token."""
        ...

    def get(self, token_digest: str) -> AccessToken | None:
        """Find a token by the digest of its value."""
        ...

    def remember_proof(self, jti: str, at: datetime) -> bool:
        """Record a proof id; False when it was seen before (a replay)."""
        ...
```

Access tokens and the proofs of possession already seen. `remember_proof` records a proof id and returns false when it was seen before (a replay).

```python
class PaymentRepository(Protocol):
    """Payments."""

    def add(self, payment: PaymentRecord) -> None:
        """Store a payment."""
        ...

    def get(self, payment_id: str, client_id: str) -> PaymentRecord | None:
        """Find a payment made for this client."""
        ...
```

Payments: add, and get one made for a given client.

```python
class LedgerRepository(Protocol):
    """The append-only ledger and the balances it keeps."""

    def post(self, transfer: Transfer, kind: str, reference: str, at: datetime) -> None:
        """Append the transfer's two postings and move the two balances in the same statement group."""
        ...

    def total(self, currency: str) -> Money:
        """The sum of every posting in `currency`; zero whenever the ledger is whole."""
        ...
```

The append-only ledger and the balances it keeps. `post` appends the transfer's two postings and moves the two balances in the same statement group; `total` is the sum of every posting, zero whenever the ledger is whole.

```python
@dataclass(frozen=True, slots=True)
class Fresh:
    """The key has not been seen: do the work."""
```

The three possible results of claiming an idempotency key. `Fresh`: the key has not been seen, do the work.

```python
@dataclass(frozen=True, slots=True)
class Replay:
    """The key was seen with this same request: answer with the stored response."""

    response: StoredResponse
```

`Replay`: the key was seen with the same request, answer with the stored response.

```python
@dataclass(frozen=True, slots=True)
class Reused:
    """The key was seen with a different request."""
```

`Reused`: the key was seen with a different request.

```python
Claim = Fresh | Replay | Reused
```

`Claim` is the type "one of these three" (a **union**, written with `|`).

```python
class IdempotencyStore(Protocol):
    """Keys of one client and their stored responses, written in the same transaction as the effect."""

    def claim(self, client_id: str, key: IdempotencyKey, request_fingerprint: str, now: datetime) -> Claim:
        """Serialize callers of one (client, key) until the unit of work ends, then say what to do with this one."""
        ...

    def store(
        self,
        client_id: str,
        key: IdempotencyKey,
        request_fingerprint: str,
        response: StoredResponse,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        """Record the response under the key."""
        ...
```

Keys of one client and their stored responses, written in the same transaction as the effect. `claim` serializes callers of one (client, key) until the unit of work ends, then says what to do with this one; `store` records the response under the key.

```python
class UnitOfWork(Protocol):
    """One database transaction: commits when the block ends normally, rolls back on an exception."""

    @property
    def accounts(self) -> AccountRepository:
        """Accounts."""
        ...

    @property
    def customers(self) -> CustomerRepository:
        """Customers."""
        ...

    @property
    def clients(self) -> ClientRepository:
        """Registered merchants."""
        ...

    @property
    def challenges(self) -> ChallengeRepository:
        """Authentication challenges."""
        ...

    @property
    def proofs(self) -> ProofRepository:
        """Proofs of strong authentication."""
        ...

    @property
    def consents(self) -> ConsentRepository:
        """Consents."""
        ...

    @property
    def tokens(self) -> TokenRepository:
        """Access tokens."""
        ...

    @property
    def payments(self) -> PaymentRepository:
        """Payments."""
        ...

    @property
    def ledger(self) -> LedgerRepository:
        """The ledger."""
        ...

    @property
    def keys(self) -> IdempotencyStore:
        """Idempotency keys."""
        ...
```

One database transaction: it commits when the block ends normally and rolls back on an exception. It exposes the ten stores (accounts, customers, clients, challenges, proofs, consents, tokens, payments, ledger and keys) as read-only properties (`@property`). The use cases see only this shape; PostgreSQL provides it, and the unit tests provide an in-memory one.

```python
UnitOfWorkFactory = Callable[[], AbstractContextManager[UnitOfWork]]
```

`UnitOfWorkFactory` is the type of a function that takes no arguments and returns something usable in a `with` block that yields a `UnitOfWork`.

```python
class HealthProbe(Protocol):
    """Reports whether a dependency is reachable."""

    def check(self) -> None:
        """Return normally when healthy; raise when not."""
        ...
```

`HealthProbe` has one method, `check()`, that returns normally when healthy and raises when not.

```python
@dataclass(frozen=True, slots=True)
class BankSettings:
    """The bank's timing rules, passed to the use cases as plain values."""

    challenge_time_to_live: timedelta
    proof_time_to_live: timedelta
    token_time_to_live: timedelta
    consent_time_to_live: timedelta
    recurring_consent_time_to_live: timedelta
    dpop_max_skew: timedelta
    idempotency_key_time_to_live: timedelta
```

The bank's timing rules, passed to the use cases as plain values (each a `timedelta`): how long a challenge, a proof, a token and a consent live (single-payment and recurring), the largest clock skew allowed for a DPoP proof, and how long an idempotency key lives.

### `src/open_banking/application/bank/queries.py` (57 lines)

```python
"""Read-only use cases of the bank: balances for the demo and the checks, and the health check."""

from __future__ import annotations

from dataclasses import dataclass

from open_banking.application.bank.ports import HealthProbe, UnitOfWorkFactory
from open_banking.domain.errors import AccountNotFoundError
from open_banking.domain.money import Money
```

Read-only use cases of the bank: balances for the demo and the checks, and the health check.

```python
class GetBalances:
    """The balance of some accounts, and the sum of the whole ledger (always zero)."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory
```

Reads the balance of some accounts and the sum of the whole ledger. The constructor keeps the transaction factory.

```python
    def counts(self) -> Counts:
        """What exists, for the demo and the checks."""
        with self._uow_factory() as uow:
            return Counts(uow.consents.count())
```

How many consents exist, for the demo and the checks, wrapped in a `Counts` object.

```python
    def execute(self, account_ids: list[str], currency: str = "USD") -> tuple[dict[str, Money], Money]:
        """Raises `AccountNotFoundError` for an unknown account."""
        with self._uow_factory() as uow:
            balances: dict[str, Money] = {}
            for account_id in account_ids:
                account = uow.accounts.get(account_id)
                if account is None:
                    raise AccountNotFoundError(f"no account {account_id!r}")
                balances[account_id] = account.balance
            return balances, uow.ledger.total(currency)
```

Opens a transaction and reads each account (an unknown account raises `AccountNotFoundError`; `{account_id!r}` prints the id with quotes). It returns a pair: a dictionary from account id to balance, and the ledger total, which must be zero.

```python
@dataclass(frozen=True, slots=True)
class Counts:
    """How many consents exist."""

    consents: int
```

A small result object with the number of consents.

```python
class CheckHealth:
    """Is every dependency reachable?"""

    def __init__(self, probes: dict[str, HealthProbe]) -> None:
        self._probes = probes

    def execute(self) -> dict[str, str]:
        """Each dependency's status, `ok` or the error's type name."""
        results: dict[str, str] = {}
        for name, probe in self._probes.items():
            try:
                probe.check()
                results[name] = "ok"
            except Exception as error:  # a health check reports every failure; it must not raise
                results[name] = type(error).__name__
        return results
```

Asks every dependency whether it is reachable. `probes` is a dictionary from a name to a probe. `execute` loops over the probes, calls `probe.check()`, and records `"ok"`, or the **type name of the error** (`type(error).__name__`) when it raised. The `except Exception` is deliberately broad, as the comment says: a health check reports every failure and must not raise.

### `src/open_banking/application/bank/enrollment.py` (49 lines)

```python
"""Setting up the bank's side: enroll a customer, register a merchant. Used by the admin program and the tests."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from open_banking.application.bank.ports import Crypto, Customer, OAuthClient, UnitOfWorkFactory
from open_banking.application.logs import fields
from open_banking.application.ports import IdSource
from open_banking.domain.errors import AccountNotFoundError
```

Setting up the bank's side: enroll a customer and register a merchant. Used by the admin program and the tests. `Mapping` is a read-only dictionary type; `Any` is the hint for "any value".

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
class EnrollCustomer:
    """A customer who can authenticate with a PIN and who owns an existing account."""

    def __init__(self, uow_factory: UnitOfWorkFactory, crypto: Crypto, ids: IdSource) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._ids = ids
```

A customer who can authenticate with a PIN and who owns an existing account. The constructor takes the transaction factory, the crypto port and the id source.

```python
    def execute(self, username: str, pin: str, account_id: str) -> Customer:
        """The PIN is stored as a slow salted hash and never again in any form."""
        with self._uow_factory() as uow:
            if uow.accounts.get(account_id) is None:
                raise AccountNotFoundError(f"no account {account_id!r}")
            customer = Customer(str(self._ids.new_id()), username, account_id, self._crypto.hash_secret(pin))
            uow.customers.add(customer)
            logger.info("customer enrolled", extra=fields(username=username, account_id=account_id))
            return customer
```

The docstring: the PIN is stored as a slow salted hash and never again in any form. The account must exist (`AccountNotFoundError` otherwise). A `Customer` is built with a new id, the username, the account and `hash_secret(pin)`, added to the store, and a log line records the enrollment with the username and account but never the PIN.

```python
class RegisterClient:
    """A merchant that may ask for payments: its secret (as a hash) and the public half of its key."""

    def __init__(self, uow_factory: UnitOfWorkFactory, crypto: Crypto) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
```

A merchant that may ask for payments: its secret (as a hash) and the public half of its signing key, written as a **JWK** (JSON Web Key: a key described as a JSON object with fields such as the curve name and the coordinates).

```python
    def execute(self, client_id: str, name: str, secret: str, public_jwk: Mapping[str, Any]) -> OAuthClient:
        """Register; the private key stays with the merchant."""
        with self._uow_factory() as uow:
            client = OAuthClient(client_id, name, self._crypto.hash_secret(secret), dict(public_jwk))
            uow.clients.add(client)
            logger.info("merchant registered", extra=fields(client_id=client_id))
            return client
```

Registers the merchant. The secret is hashed (`hash_secret`); the public key is copied into a plain dictionary (`dict(public_jwk)`); the private key stays with the merchant and never reaches the bank.

### `src/open_banking/application/bank/access.py` (88 lines)

```python
"""Access for merchants: client credentials exchanged for a token bound to the merchant's own key (DPoP)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from open_banking.application.bank.ports import AccessToken, BankSettings, Crypto, UnitOfWorkFactory
from open_banking.application.logs import fields
from open_banking.application.ports import Clock
from open_banking.domain.errors import ClientAuthenticationError
```

Access for merchants: client credentials exchanged for a token bound to the merchant's own key (**DPoP**, demonstration of proof of possession). A normal bearer token works for anyone who steals it; a DPoP-bound token is useless without the merchant's private key, because each request must also carry a fresh signed proof.

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
@dataclass(frozen=True, slots=True)
class IssuedToken:
    """An access token, shown once, and how long it lasts."""

    access_token: str
    expires_in_seconds: int
```

An access token, shown once, and how long it lasts in seconds.

```python
class IssueToken:
    """Authenticate the merchant (secret and proof of possession of its key) and issue a key-bound token."""
```

Authenticates the merchant (secret **and** proof of possession of its key) and issues a key-bound token.

```python
    def __init__(self, uow_factory: UnitOfWorkFactory, crypto: Crypto, clock: Clock, settings: BankSettings) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._clock = clock
        self._settings = settings
        self._decoy = crypto.hash_secret("not-a-client-secret")
```

Keeps its collaborators and computes a `_decoy` hash of a made-up secret once. The decoy lets the code verify a secret even for an unknown client, so a wrong client id takes as long as a wrong secret and the timing reveals nothing.

```python
    def execute(self, client_id: str, client_secret: str, dpop_proof: str, token_url: str) -> IssuedToken:
        """Raises `ClientAuthenticationError` for a wrong secret, a proof signed by another key, or a replayed proof."""
        with self._uow_factory() as uow:
            client = uow.clients.get(client_id)
            secret_ok = self._crypto.verify_secret(client_secret, client.secret_hash if client else self._decoy)
            if client is None or not secret_ok:
                logger.warning("merchant credential refused", extra=fields(client_id=client_id))
                raise ClientAuthenticationError("invalid merchant credential")
            now = self._clock.now()
            claims = self._crypto.verify_dpop_proof(
                dpop_proof, client.public_jwk, method="POST", url=token_url, now=now, access_token=None
            )
            if not uow.tokens.remember_proof(claims.jti, now):
                raise ClientAuthenticationError("the proof of possession was already used")
            token = self._crypto.new_token()
            uow.tokens.add(
                AccessToken(
                    self._crypto.token_digest(token),
                    client_id,
                    self._crypto.thumbprint(client.public_jwk),
                    now,
                    now + self._settings.token_time_to_live,
                )
            )
            logger.info("token issued", extra=fields(client_id=client_id))
            return IssuedToken(token, int(self._settings.token_time_to_live.total_seconds()))
```

In one transaction: find the client; verify the secret in constant time against the client's hash (or the decoy); an unknown client or a wrong secret logs a warning (client id only, never the secret) and raises `ClientAuthenticationError("invalid merchant credential")`, the same message for both. Then the proof of possession is verified against the client's registered public key for `POST` to the token URL, with no access token yet. `remember_proof` records the proof's unique id (`jti`); if it was seen before, the proof is a replay and is refused. Finally a new opaque token is made, **only its digest** is stored together with the client id, the thumbprint of the client's key (the token's binding) and the times, and the token is returned once with its lifetime in seconds.

```python
class AuthenticateCaller:
    """Check a request from a merchant: a valid, unexpired token and a fresh proof that the caller holds its key."""

    def __init__(self, uow_factory: UnitOfWorkFactory, crypto: Crypto, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._clock = clock
```

Checks a request from a merchant: a valid, unexpired token and a fresh proof that the caller holds its key.

```python
    def execute(self, authorization: str | None, dpop_proof: str | None, *, method: str, url: str) -> str:
        """Return the merchant's client id, or raise `ClientAuthenticationError`."""
        scheme, _, token = (authorization or "").partition(" ")
        if scheme != "DPoP" or not token or not dpop_proof:
            raise ClientAuthenticationError("invalid merchant credential")
        with self._uow_factory() as uow:
            now = self._clock.now()
            record = uow.tokens.get(self._crypto.token_digest(token))
            if record is None or now >= record.expires_at:
                raise ClientAuthenticationError("invalid merchant credential")
            client = uow.clients.get(record.client_id)
            if client is None or self._crypto.thumbprint(client.public_jwk) != record.key_thumbprint:
                raise ClientAuthenticationError("invalid merchant credential")
            claims = self._crypto.verify_dpop_proof(
                dpop_proof, client.public_jwk, method=method, url=url, now=now, access_token=token
            )
            if not uow.tokens.remember_proof(claims.jti, now):
                raise ClientAuthenticationError("the proof of possession was already used")
            return record.client_id
```

Returns the merchant's client id, or raises `ClientAuthenticationError`. The `Authorization` header must look like `DPoP <token>`: `partition(" ")` splits at the first space into scheme, separator and token (`_` ignores the separator); a different scheme, an empty token or a missing proof is refused. In a transaction: look the token up by its digest (an unknown or expired token is refused), load the client, and check that the client's key thumbprint equals the one the token was bound to (so a token can never be used with another key). Then the proof is verified for this request's method and URL **and** against the token (the proof contains a hash of the token), and its `jti` must be new. Every failure gives the same message, `invalid merchant credential`.

### `src/open_banking/application/bank/authentication.py` (125 lines)

```python
"""Strong customer authentication at the bank: a PIN, then a one-time code, between the customer and the bank only."""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from datetime import datetime

from open_banking.application.bank.ports import BankSettings, Crypto, OtpChannel, UnitOfWorkFactory
from open_banking.application.logs import fields
from open_banking.application.ports import Clock, IdSource
from open_banking.domain.authentication import MAX_ATTEMPTS, AttemptOutcome, Challenge
from open_banking.domain.consent import AuthenticationProof, ConsentTerms
from open_banking.domain.errors import (
    AccountNotFoundError,
    AccountNotOwnedError,
    AuthenticationFailedError,
    ChallengeNotFoundError,
    UnknownClientError,
)
```

Strong customer authentication at the bank: a PIN, then a one-time code, between the customer and the bank only (the merchant never sees either). `hmac` offers `compare_digest`, a comparison whose time does not depend on where two values differ.

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
@dataclass(frozen=True, slots=True)
class ChallengeStarted:
    """A challenge was created and a code sent to the customer's device."""

    challenge_id: str
    expires_at: datetime
```

A challenge was created and a code sent to the customer's device: its id and expiry.

```python
@dataclass(frozen=True, slots=True)
class Verification:
    """The result of entering a code. `proof` is the bearer value of the proof, shown once, when the code was right."""

    outcome: AttemptOutcome
    proof: str | None
    proof_expires_at: datetime | None
    attempts_left: int
```

The result of entering a code: the outcome, `proof` (the bearer value of the proof, shown once, when the code was right, else `None`), when the proof expires and how many attempts are left.

```python
class StartChallenge:
    """Check the customer's PIN and send a code, for exactly the terms the customer is about to approve."""
```

Checks the customer's PIN and sends a code, for exactly the terms the customer is about to approve.

```python
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        crypto: Crypto,
        otp: OtpChannel,
        clock: Clock,
        ids: IdSource,
        settings: BankSettings,
    ) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._otp = otp
        self._clock = clock
        self._ids = ids
        self._settings = settings
        self._decoy = crypto.hash_secret("not-a-customer-pin")  # so an unknown user costs the same as a wrong PIN
```

Keeps the collaborators (including the one-time-code channel) and builds `_decoy`, a hash of a made-up PIN, so an unknown user costs the same time as a wrong PIN (the comment says so).

```python
    def execute(self, username: str, pin: str, terms: ConsentTerms) -> ChallengeStarted:
        """Raises `AuthenticationFailedError` for an unknown user or a wrong PIN (one answer for both)."""
        with self._uow_factory() as uow:
            customer = uow.customers.by_username(username)
            pin_ok = self._crypto.verify_secret(pin, customer.pin_hash if customer else self._decoy)
            if customer is None or not pin_ok:
                logger.warning("authentication failed", extra=fields(username=username))
                raise AuthenticationFailedError("the username or PIN is not right")
            if terms.payer_account != customer.account_id:
                raise AccountNotOwnedError("the payer account does not belong to this customer")
            if uow.clients.get(terms.client_id) is None:
                raise UnknownClientError(f"no merchant is registered as {terms.client_id!r}")
            if uow.accounts.get(terms.payee_account) is None:
                raise AccountNotFoundError(f"no account {terms.payee_account!r}")
            now = self._clock.now()
            challenge_id = str(self._ids.new_id())
            code = self._crypto.new_code()
            challenge = Challenge(
                challenge_id=challenge_id,
                customer_id=customer.customer_id,
                terms_digest=terms.digest(),
                code_digest=self._crypto.code_digest(challenge_id, code),
                created_at=now,
                expires_at=now + self._settings.challenge_time_to_live,
            )
            uow.challenges.add(challenge)
            self._otp.send(customer.customer_id, code, "approve a payment consent")
            logger.info("challenge started", extra=fields(challenge_id=challenge_id, customer_id=customer.customer_id))
            return ChallengeStarted(challenge_id, challenge.expires_at)
```

In a transaction: find the customer by username; verify the PIN against the stored hash or the decoy; an unknown user or a wrong PIN logs a warning (username only) and raises `AuthenticationFailedError` (one answer for both). Then three consistency checks on the **terms**: the payer account must be the customer's own (`AccountNotOwnedError`), the merchant must be registered (`UnknownClientError`) and the payee account must exist (`AccountNotFoundError`). Then a challenge id and a fresh six-digit code are made; the `Challenge` stores the **digest of the terms** and the **keyed digest of the code** (never the code); it is added and the code is handed to the channel with the purpose `approve a payment consent`. The log line records the challenge and customer ids, not the code.

```python
class VerifyChallenge:
    """Check the one-time code. A right code issues a proof for the challenge's terms; a wrong one counts."""

    def __init__(self, uow_factory: UnitOfWorkFactory, crypto: Crypto, clock: Clock, settings: BankSettings) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._clock = clock
        self._settings = settings
```

Checks the one-time code. A right code issues a proof for the challenge's terms; a wrong one counts.

```python
    def execute(self, challenge_id: str, code: str) -> Verification:
        """Wrong codes, locks and expiry are results, not errors, so the attempt is stored."""
        with self._uow_factory() as uow:
            challenge = uow.challenges.get_for_update(challenge_id)
            if challenge is None:
                raise ChallengeNotFoundError(f"no challenge {challenge_id!r}")
            now = self._clock.now()
            matches = hmac.compare_digest(self._crypto.code_digest(challenge_id, code), challenge.code_digest)
            updated, outcome = challenge.attempt(matches, now)
            uow.challenges.save(updated)
            if outcome is not AttemptOutcome.VERIFIED:
                logger.warning("code not accepted", extra=fields(challenge_id=challenge_id, outcome=outcome.value))
                return Verification(outcome, None, None, max(MAX_ATTEMPTS - updated.attempts, 0))
            token = self._crypto.new_token()
            expires_at = now + self._settings.proof_time_to_live
            uow.proofs.add(
                AuthenticationProof(
                    self._crypto.token_digest(token), challenge.customer_id, challenge.terms_digest, expires_at
                )
            )
            logger.info("strong authentication succeeded", extra=fields(challenge_id=challenge_id))
            return Verification(outcome, token, expires_at, MAX_ATTEMPTS)
```

The docstring: wrong codes, locks and expiry are *results*, not errors, because the attempt must be stored (an exception would roll it back). In a transaction: lock and load the challenge (unknown: `ChallengeNotFoundError`); compare the digest of the entered code with the stored digest using `hmac.compare_digest`; ask the domain's `Challenge.attempt` for the updated challenge and the outcome; **save** the updated challenge. If the outcome is not `VERIFIED`, log a warning and return it with the attempts left (`max(MAX_ATTEMPTS - updated.attempts, 0)`). If it is verified, a new opaque token is made, only its digest is stored as the `AuthenticationProof` (customer, terms digest, expiry), and the token is returned once.

### `src/open_banking/application/bank/consents.py` (82 lines)

```python
"""Consent at the bank: granted only against a proof of strong authentication, revocable by the customer."""

from __future__ import annotations

import logging

from open_banking.application.bank.ports import BankSettings, Crypto, UnitOfWorkFactory
from open_banking.application.logs import fields
from open_banking.application.ports import Clock, IdSource
from open_banking.domain.consent import Consent, ConsentScope, ConsentTerms
from open_banking.domain.errors import (
    AccountNotFoundError,
    AuthenticationFailedError,
    ConsentNotFoundError,
    InvalidProofError,
    UnknownClientError,
)
```

Consent at the bank: granted only against a proof of strong authentication, revocable by the customer.

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
class GrantConsent:
    """Create a consent from a proof of authentication. The proof is spent in the same transaction."""
```

Creates a consent from a proof of authentication. The docstring: the proof is spent in the same transaction.

```python
    def __init__(
        self, uow_factory: UnitOfWorkFactory, crypto: Crypto, clock: Clock, ids: IdSource, settings: BankSettings
    ) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._clock = clock
        self._ids = ids
        self._settings = settings
```

Keeps the collaborators, including the settings that hold the consent lifetimes.

```python
    def execute(self, proof_token: str, terms: ConsentTerms) -> Consent:
        """Raises `InvalidProofError` when the proof is unknown, expired, used, or for other terms."""
        with self._uow_factory() as uow:
            proof = uow.proofs.get_for_update(self._crypto.token_digest(proof_token))
            if proof is None:
                raise InvalidProofError("the proof of authentication is not known")
            if uow.clients.get(terms.client_id) is None:
                raise UnknownClientError(f"no merchant is registered as {terms.client_id!r}")
            if uow.accounts.get(terms.payee_account) is None:
                raise AccountNotFoundError(f"no account {terms.payee_account!r}")
            time_to_live = (
                self._settings.consent_time_to_live
                if terms.scope is ConsentScope.SINGLE_PAYMENT
                else self._settings.recurring_consent_time_to_live
            )
            consent = Consent.grant(str(self._ids.new_id()), proof, terms, self._clock.now(), time_to_live)
            uow.proofs.mark_consumed(proof.proof_id)
            uow.consents.add(consent)
            logger.info(
                "consent granted",
                extra=fields(consent_id=consent.consent_id, scope=terms.scope.value, client_id=terms.client_id),
            )
            return consent
```

Raises `InvalidProofError` when the proof is unknown, expired, used, or for other terms. In a transaction: lock and load the proof by the digest of the bearer value (unknown: refused); check the merchant is registered and the payee account exists; choose the lifetime (the single-payment lifetime or the longer recurring one) with a conditional expression; `Consent.grant` (in the domain) does the proof checks and builds the consent; the proof is marked consumed and the consent stored, **in the same transaction**, so a proof can create at most one consent even if two requests race. The log line records the consent id, the scope and the merchant.

```python
class RevokeConsent:
    """The customer withdraws a consent, independently of the merchant. Revoking again changes nothing."""
```

The customer withdraws a consent, independently of the merchant. Revoking again changes nothing.

```python
    def __init__(self, uow_factory: UnitOfWorkFactory, crypto: Crypto, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._clock = clock
        self._decoy = crypto.hash_secret("not-a-customer-pin")
```

Keeps the collaborators and a decoy hash, as in the authentication code.

```python
    def execute(self, username: str, pin: str, consent_id: str) -> Consent:
        """Needs the customer's PIN (withdrawing authority needs one factor, granting it needs two)."""
        with self._uow_factory() as uow:
            customer = uow.customers.by_username(username)
            pin_ok = self._crypto.verify_secret(pin, customer.pin_hash if customer else self._decoy)
            if customer is None or not pin_ok:
                raise AuthenticationFailedError("the username or PIN is not right")
            consent = uow.consents.get_for_update(consent_id)
            if consent is None or consent.customer_id != customer.customer_id:
                raise ConsentNotFoundError(f"no consent {consent_id!r}")
            revoked = consent.revoked(self._clock.now())
            if revoked is not consent:
                uow.consents.save(revoked)
                logger.info("consent revoked", extra=fields(consent_id=consent_id))
            return revoked
```

The docstring: it needs the customer's PIN, because withdrawing authority needs one factor while granting it needs two. The PIN is verified (one error for an unknown user or a wrong PIN); the consent is locked and loaded; a missing consent, or one that belongs to another customer, gives the same `ConsentNotFoundError` (so existence is not revealed). `consent.revoked(now)` returns the same object if it was already revoked, so `revoked is not consent` tells whether anything changed; only then is it saved and logged.

### `src/open_banking/application/bank/payments.py` (197 lines)

```python
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
```

Payment initiation: money moves only under a valid consent, exactly once, for the amount that was signed. New imports: `Any`, `datetime` and the domain pieces for fingerprints, the transfer and the canonical request.

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
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
```

A payment as the API shows it: a dictionary with the id, the status `POSTED`, the consent, the two accounts, the amount in minor units, the currency, the formatted amount and the time as ISO text.

```python
@dataclass(frozen=True, slots=True)
class PaymentResult:
    """The response to send, and whether it was replayed from the key's stored answer."""

    response: StoredResponse
    replayed: bool
```

The response to send, and whether it was replayed from the key's stored answer.

```python
class InitiatePayment:
    """Make the payment a merchant asks for, if and only if a consent covers it.

    In one transaction: take the (merchant, key) lock; check the request signature; lock the consent and check every
    rule on it; lock the payer's account; post the balanced transfer; store the answer under the key. A replay returns
    the stored answer and checks nothing again. Any refusal rolls everything back and stores nothing.
    """
```

The docstring lists the order inside one transaction: take the (merchant, key) lock; check the request signature; lock the consent and check every rule on it; lock the payer's account; post the balanced transfer; store the answer under the key. A replay returns the stored answer and checks nothing again. Any refusal rolls everything back and stores nothing.

```python
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
```

Keeps the executor, crypto, clock and ids. `del uow_factory` removes the unused parameter name on purpose (the comment: the executor owns the transaction), which keeps the constructor signature the same for the container that builds it.

```python
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
```

Raises `PaymentRefusedError` with one distinct reason for every refusal. It builds the request **fingerprint** (a hash of the operation name and the fields that identify the request: the consent, the payee, the amount and the currency), defines the nested function `work(uow)` that performs the payment, and passes it to the executor together with the merchant id, the key and the fingerprint. The executor decides whether to run `work` (a new key), replay (the same request) or refuse (a different request with the same key). The result is wrapped in a `PaymentResult`.

```python
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
```

The effect, running inside the executor's transaction. (1) Load the merchant (unknown: `ClientAuthenticationError`). (2) Rebuild the canonical bytes the merchant should have signed, from the **received** values and the key, and verify the signature with the merchant's public key; a missing or wrong signature is refused as `REQUEST_NOT_AS_SIGNED`, so a tampered amount cannot pass. (3) Lock and load the consent (missing: `CONSENT_NOT_FOUND`). (4) Work out how much was already spent in the period. (5) Ask the domain `refusal_for` for the first reason this payment may not happen; if there is one, raise a refusal with the explaining sentence. (6) Lock **both** accounts in a fixed order (`sorted({...})` of the two ids), the comment explains why: two payments in opposite directions then cannot wait for each other (deadlock). A missing account is an error. (7) The payer's balance must cover the amount (`INSUFFICIENT_FUNDS`). (8) Make the `PaymentRecord`, post the balanced `Transfer` under the kind `PAYMENT`, store the payment, and for a single-payment consent mark it used. (9) Log `payment posted` and return the stored response (status 201 and the payment body).

```python
    @staticmethod
    def _spent_in_period(
        uow: UnitOfWork, consent_id: str, scope: ConsentScope, period: Any, now: datetime, amount: Money
    ) -> Money:
        if scope is not ConsentScope.RECURRING or period is None:
            return Money.zero(amount.currency)
        return uow.consents.spent_since(consent_id, now - period, amount.currency)
```

For a recurring consent, what was already moved under it in the last period (`now - period`); for any other consent, zero in the request's currency. `@staticmethod` (above) means the method does not use `self`.

```python
    @staticmethod
    def _refused(refusal: Refusal, message: str, client_id: str, request: PaymentRequest) -> PaymentRefusedError:
        logger.warning(
            "payment refused",
            extra=fields(refusal=refusal.value, client_id=client_id, consent_id=request.consent_id),
        )
        return PaymentRefusedError(refusal.value, message)
```

Builds the `PaymentRefusedError` for a refusal and logs a warning with the reason, the merchant and the consent. It returns the error so the caller can write `raise self._refused(...)`.

```python
class GetPayment:
    """A merchant reads a payment it initiated."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory
```

A merchant reads a payment it initiated.

```python
    def execute(self, client_id: str, payment_id: str) -> dict[str, Any]:
        """Raises `PaymentNotFoundError` for an unknown payment or one made for another merchant."""
        with self._uow_factory() as uow:
            payment = uow.payments.get(payment_id, client_id)
            if payment is None:
                raise PaymentNotFoundError(f"no payment {payment_id!r}")
            return payment_body(payment)
```

Looks the payment up by id **and** merchant; an unknown payment, or one made for another merchant, gives the same `PaymentNotFoundError`.

### `src/open_banking/application/merchant/checkout.py` (180 lines)

```python
"""The merchant's side: a checkout, and the payment it initiates under a consent the customer gave at the bank."""

from __future__ import annotations

import dataclasses
import logging
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from open_banking.application.logs import fields
from open_banking.application.ports import IdSource
from open_banking.domain.errors import OpenBankingError
from open_banking.domain.money import Money
```

The merchant's side: a checkout, and the payment it initiates under a consent the customer gave at the bank. `threading` provides a lock; `Protocol` describes shapes; `StrEnum` makes an enum of texts.

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
class BankUnavailableError(OpenBankingError):
    """The bank did not answer in time on every attempt. The payment may or may not have been made; ask again."""

    code = "bank_unavailable"
```

Raised when the bank did not answer in time on every attempt. The docstring: the payment may or may not have been made; ask again. It is defined next to the code that raises it and has its own `code`.

```python
class CheckoutNotFoundError(OpenBankingError):
    """No checkout has this id."""

    code = "checkout_not_found"
```

No checkout has this id.

```python
class CheckoutStatus(StrEnum):
    """Where a checkout is."""

    AWAITING_CONSENT = "AWAITING_CONSENT"
    PAID = "PAID"
    UNKNOWN = "UNKNOWN"  # the bank did not answer; completing again with the same consent is safe
```

Where a checkout is: `AWAITING_CONSENT`, `PAID` or `UNKNOWN` (the bank did not answer; completing again with the same consent is safe, the comment says).

```python
@dataclass(frozen=True, slots=True)
class Paid:
    """The bank made the payment."""

    payment_id: str
```

The bank made the payment: its payment id.

```python
@dataclass(frozen=True, slots=True)
class Refused:
    """The bank refused, with its reason."""

    code: str
    message: str
```

The bank refused, with its reason code and message.

```python
class BankGateway(Protocol):
    """The merchant's way to the bank: authenticated, key-bound, signed, and retried with the same key."""

    def initiate_payment(
        self, *, consent_id: str, payee_account: str, amount: Money, idempotency_key: str
    ) -> Paid | Refused:
        """Raises `BankUnavailableError` when every attempt timed out."""
        ...
```

The merchant's way to the bank: authenticated, key-bound, signed, and retried with the same key. `initiate_payment` takes only named arguments and returns `Paid` or `Refused`; the docstring says it raises `BankUnavailableError` when every attempt timed out.

```python
@dataclass(frozen=True, slots=True)
class MerchantProfile:
    """Who the merchant is at the bank, and where it is paid."""

    client_id: str
    payee_account: str
```

Who the merchant is at the bank (`client_id`) and which account it is paid into.

```python
@dataclass(frozen=True, slots=True)
class Checkout:
    """One purchase. `refusal` holds the bank's last reason when the last attempt was refused."""

    checkout_id: str
    amount: Money
    status: CheckoutStatus
    consent_id: str | None = None
    payment_id: str | None = None
    refusal: Refused | None = None
```

One purchase: its id, the amount, the status and, when known, the consent id, the payment id and the bank's last refusal.

```python
class CheckoutRepository(Protocol):
    """The merchant's checkouts."""

    def add(self, checkout: Checkout) -> None:
        """Store a new checkout."""
        ...

    def get(self, checkout_id: str) -> Checkout | None:
        """Find one."""
        ...

    def save(self, checkout: Checkout) -> None:
        """Write a changed checkout."""
        ...
```

The merchant's checkouts: add, get and save.

```python
class InMemoryCheckouts:
    """`CheckoutRepository` in memory. The merchant's own state is small and this build does not give it a database."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Checkout] = {}

    def add(self, checkout: Checkout) -> None:
        """Store a new checkout."""
        with self._lock:
            self._items[checkout.checkout_id] = checkout

    def get(self, checkout_id: str) -> Checkout | None:
        """Find one."""
        with self._lock:
            return self._items.get(checkout_id)

    def save(self, checkout: Checkout) -> None:
        """Write a changed checkout."""
        with self._lock:
            self._items[checkout.checkout_id] = checkout
```

`CheckoutRepository` kept in a dictionary. The docstring explains the choice: the merchant's own state is small, and this build does not give it a database. A `threading.Lock` makes access safe from several threads (`with self._lock:` holds it for the block).

```python
class CreateCheckout:
    """Start a purchase. The customer is then sent to the bank with the terms to approve."""

    def __init__(self, checkouts: CheckoutRepository, ids: IdSource) -> None:
        self._checkouts = checkouts
        self._ids = ids
```

Starts a purchase. The customer is then sent to the bank with the terms to approve.

```python
    def execute(self, amount: Money) -> Checkout:
        """A new checkout awaiting the customer's consent."""
        checkout = Checkout(str(self._ids.new_id()), amount, CheckoutStatus.AWAITING_CONSENT)
        self._checkouts.add(checkout)
        logger.info("checkout created", extra=fields(checkout_id=checkout.checkout_id, amount=amount.format()))
        return checkout
```

Makes a checkout with a new id, the amount and the status `AWAITING_CONSENT`, stores it and logs it.

```python
class CompleteCheckout:
    """Initiate the payment with the consent id the customer brought back from the bank.

    The idempotency key is built from the checkout and the consent, so a retry of the same attempt always sends the
    same key and a different consent gets a different one. Completing a paid checkout returns it unchanged.
    """

    def __init__(self, checkouts: CheckoutRepository, gateway: BankGateway, profile: MerchantProfile) -> None:
        self._checkouts = checkouts
        self._gateway = gateway
        self._profile = profile
```

Initiates the payment with the consent id the customer brought back from the bank. The docstring explains the **idempotency key**: it is built from the checkout and the consent, so a retry of the same attempt always sends the same key and a different consent gets a different one. Completing a paid checkout returns it unchanged.

```python
    def execute(self, checkout_id: str, consent_id: str) -> Checkout:
        """Raises `CheckoutNotFoundError`, or `BankUnavailableError` when the bank cannot be reached."""
        checkout = self._checkouts.get(checkout_id)
        if checkout is None:
            raise CheckoutNotFoundError(f"no checkout {checkout_id!r}")
        if checkout.status is CheckoutStatus.PAID:
            return checkout
        try:
            result = self._gateway.initiate_payment(
                consent_id=consent_id,
                payee_account=self._profile.payee_account,
                amount=checkout.amount,
                idempotency_key=f"checkout-{checkout.checkout_id}-{consent_id}",
            )
        except BankUnavailableError:
            self._checkouts.save(dataclasses.replace(checkout, status=CheckoutStatus.UNKNOWN, consent_id=consent_id))
            raise
        if isinstance(result, Paid):
            paid = dataclasses.replace(
                checkout, status=CheckoutStatus.PAID, consent_id=consent_id, payment_id=result.payment_id, refusal=None
            )
            self._checkouts.save(paid)
            logger.info("checkout paid", extra=fields(checkout_id=checkout_id, payment_id=result.payment_id))
            return paid
        refused = dataclasses.replace(
            checkout, status=CheckoutStatus.AWAITING_CONSENT, consent_id=consent_id, refusal=result
        )
        self._checkouts.save(refused)
        logger.warning("checkout refused", extra=fields(checkout_id=checkout_id, refusal=result.code))
        return refused
```

Looks up the checkout (`CheckoutNotFoundError`); a paid checkout is returned unchanged. Otherwise the gateway is asked to initiate the payment with the key `checkout-<checkout id>-<consent id>`. If the bank is unavailable, the checkout is saved with the status `UNKNOWN` and the consent id (`dataclasses.replace` copies it with changes) and the error is raised again (`raise` with nothing after it). If the result is `Paid`, the checkout is saved as `PAID` with the payment id and a log line is written. Otherwise (a refusal) the checkout goes back to `AWAITING_CONSENT` with the bank's reason stored, a warning is logged and the checkout is returned.

## Try it yourself

1. Run the unit tests of the use cases (no database needed):

   ```bash
   .venv/bin/python -m pytest tests/unit/application -q
   ```

   Expected: all pass.

2. See the whole story run end to end with the database: `make up` then `make run`; read the numbered steps that print each use case's result.

3. Read the refusal for a payment above the consent: in `tests/unit/application/test_payments.py` find the test for `amount_exceeds_consent` and run it with `-k exceeds -v`.

## Mistakes people make

- **Storing the one-time code or a PIN.** Only a hash or a keyed digest is stored.
- **Telling the caller whether a user or a merchant exists.** Unknown user and wrong PIN give one answer; unknown and foreign consents give one answer.
- **Locking accounts in the order the request names them.** Two opposite payments could then deadlock; the code sorts the ids first.
- **Treating a wrong code as an exception.** The attempt must be saved, so it is a result.
- **Verifying a signature over the fields the client claims.** The bank rebuilds the canonical bytes itself from the received request and the key.

## Check yourself

1. What binds an access token to a merchant's key? *The token record stores the thumbprint of the merchant's public key, and each request must carry a proof signed by that key.*
2. Why does `StartChallenge` hash a decoy PIN? *So an unknown user takes as long as a wrong PIN.*
3. What does the idempotent executor do for a repeated request? *Returns the stored response without running the payment again.*
4. Which two things are in the same transaction as a payment? *The idempotency key and the stored response (with the ledger postings and the payment record).*
5. Why does the merchant's idempotency key include the consent id? *A different consent is a different attempt and must get a different key.*

## Further reading

- Python 3.12 `typing.Protocol`: https://docs.python.org/3.12/library/typing.html#typing.Protocol
- Python 3.12 `hmac.compare_digest`: https://docs.python.org/3.12/library/hmac.html#hmac.compare_digest
- RFC 9449, OAuth 2.0 Demonstrating Proof of Possession (DPoP): https://www.rfc-editor.org/rfc/rfc9449
- Python 3.12 `logging`: https://docs.python.org/3.12/library/logging.html
