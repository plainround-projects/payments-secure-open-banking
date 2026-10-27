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


class FixedClock:
    """A clock that moves only when told to."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class SequentialIds:
    """Predictable UUIDs: 00000000-0000-4000-8000-000000000001, ...0002, ..."""

    def __init__(self) -> None:
        self._counter = 0
        self._lock = threading.Lock()

    def new_id(self) -> UUID:
        with self._lock:
            self._counter += 1
            return UUID(f"00000000-0000-4000-8000-{self._counter:012d}")


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

    def open_account(self, account_id: str, minor: int, *, vault: bool = False) -> None:
        self.accounts[account_id] = Account(account_id, account_id, Money(minor, "USD"), vault)
        if minor:
            self.postings.append(("FUNDING", account_id, account_id, minor))
            self.postings.append(("FUNDING", account_id, "acct-vault", -minor))

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

    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        with self._lock:
            snapshot = self._state()
            try:
                yield _InMemoryUnitOfWork(self)
            except BaseException:
                self._restore(snapshot)
                raise

    def ledger_sum(self) -> int:
        return sum(row[3] for row in self.postings)

    def balance(self, account_id: str) -> int:
        return self.accounts[account_id].balance.minor

    def customers_by_name(self, username: str) -> Customer:
        return next(c for c in self.customers.values() if c.username == username)


class _Accounts:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def get(self, account_id: str) -> Account | None:
        return self._db.accounts.get(account_id)

    def get_for_update(self, account_id: str) -> Account | None:
        return self._db.accounts.get(account_id)


class _Customers:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def by_username(self, username: str) -> Customer | None:
        return next((c for c in self._db.customers.values() if c.username == username), None)

    def by_id(self, customer_id: str) -> Customer | None:
        return self._db.customers.get(customer_id)

    def add(self, customer: Customer) -> None:
        self._db.customers[customer.customer_id] = customer


class _Clients:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def get(self, client_id: str) -> OAuthClient | None:
        return self._db.clients.get(client_id)

    def add(self, client: OAuthClient) -> None:
        self._db.clients[client.client_id] = client


class _Challenges:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, challenge: Challenge) -> None:
        self._db.challenges[challenge.challenge_id] = challenge

    def get_for_update(self, challenge_id: str) -> Challenge | None:
        return self._db.challenges.get(challenge_id)

    def save(self, challenge: Challenge) -> None:
        self._db.challenges[challenge.challenge_id] = challenge


class _Proofs:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, proof: AuthenticationProof) -> None:
        self._db.proofs[proof.proof_id] = proof

    def get_for_update(self, proof_id: str) -> AuthenticationProof | None:
        return self._db.proofs.get(proof_id)

    def mark_consumed(self, proof_id: str) -> None:
        self._db.proofs[proof_id] = replace(self._db.proofs[proof_id], consumed=True)


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


class _Payments:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, payment: PaymentRecord) -> None:
        self._db.payments[payment.payment_id] = payment

    def get(self, payment_id: str, client_id: str) -> PaymentRecord | None:
        payment = self._db.payments.get(payment_id)
        return payment if payment is not None and payment.client_id == client_id else None


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
