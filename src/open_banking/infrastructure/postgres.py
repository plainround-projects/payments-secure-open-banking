"""PostgreSQL: the connection pool, the unit of work, and the repositories behind the ports."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

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
from open_banking.domain.authentication import Challenge, ChallengeStatus
from open_banking.domain.consent import AuthenticationProof, Consent, ConsentScope, ConsentTerms
from open_banking.domain.idempotency import IdempotencyKey, StoredResponse
from open_banking.domain.ledger import Transfer
from open_banking.domain.money import Money
from open_banking.infrastructure.settings import PostgresSettings


def conninfo(settings: PostgresSettings) -> str:
    """The libpq connection string for `settings`; sessions run in UTC."""
    return make_conninfo(
        host=settings.host,
        port=settings.port,
        dbname=settings.database,
        user=settings.user,
        password=settings.password,
        connect_timeout=settings.connect_timeout_seconds,
        application_name="open-banking",
        options="-c timezone=UTC",
    )


def open_pool(settings: PostgresSettings) -> ConnectionPool[psycopg.Connection[Any]]:
    """Open a pool and wait until it holds a working connection, or fail within the connect timeout."""
    pool: ConnectionPool[psycopg.Connection[Any]] = ConnectionPool(
        conninfo(settings), min_size=1, max_size=settings.pool_max_size, open=False, timeout=15.0
    )
    pool.open(wait=True, timeout=float(settings.connect_timeout_seconds) * 6)
    return pool


class PostgresAccounts:
    """`AccountRepository` over `bank_accounts`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def get(self, account_id: str) -> Account | None:
        """Read an account."""
        return self._read(
            "SELECT account_id, owner, currency, vault, balance_minor FROM bank_accounts WHERE account_id = %s",
            account_id,
        )

    def get_for_update(self, account_id: str) -> Account | None:
        """Read and lock the account until the transaction ends."""
        return self._read(
            "SELECT account_id, owner, currency, vault, balance_minor FROM bank_accounts "
            "WHERE account_id = %s FOR UPDATE",
            account_id,
        )

    def _read(self, sql: str, account_id: str) -> Account | None:
        row = self._connection.execute(sql, (account_id,)).fetchone()
        if row is None:
            return None
        account, owner, currency, vault, balance = row
        return Account(account, owner, Money(balance, currency.strip()), vault)


class PostgresCustomers:
    """`CustomerRepository` over `customers`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def by_username(self, username: str) -> Customer | None:
        """Find by username."""
        return self._read("username = %s", username)

    def by_id(self, customer_id: str) -> Customer | None:
        """Find by id."""
        return self._read("customer_id = %s", customer_id)

    def add(self, customer: Customer) -> None:
        """Enroll a customer."""
        self._connection.execute(
            "INSERT INTO customers (customer_id, username, account_id, pin_hash) VALUES (%s, %s, %s, %s)",
            (customer.customer_id, customer.username, customer.account_id, customer.pin_hash),
        )

    def _read(self, condition: str, value: str) -> Customer | None:
        row = self._connection.execute(
            f"SELECT customer_id, username, account_id, pin_hash FROM customers WHERE {condition}", (value,)
        ).fetchone()
        return None if row is None else Customer(str(row[0]), row[1], row[2], row[3])


class PostgresClients:
    """`ClientRepository` over `oauth_clients`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def get(self, client_id: str) -> OAuthClient | None:
        """Find a client."""
        row = self._connection.execute(
            "SELECT client_id, name, secret_hash, public_jwk FROM oauth_clients WHERE client_id = %s", (client_id,)
        ).fetchone()
        return None if row is None else OAuthClient(row[0], row[1], row[2], row[3])

    def add(self, client: OAuthClient) -> None:
        """Register a client."""
        self._connection.execute(
            "INSERT INTO oauth_clients (client_id, name, secret_hash, public_jwk) VALUES (%s, %s, %s, %s)",
            (client.client_id, client.name, client.secret_hash, Jsonb(dict(client.public_jwk))),
        )


class PostgresChallenges:
    """`ChallengeRepository` over `authentication_challenges`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def add(self, challenge: Challenge) -> None:
        """Store a new challenge."""
        self._connection.execute(
            "INSERT INTO authentication_challenges (challenge_id, customer_id, terms_digest, code_digest, created_at, "
            "expires_at, attempts, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                challenge.challenge_id,
                challenge.customer_id,
                challenge.terms_digest,
                challenge.code_digest,
                challenge.created_at,
                challenge.expires_at,
                challenge.attempts,
                challenge.status.value,
            ),
        )

    def get_for_update(self, challenge_id: str) -> Challenge | None:
        """Read and lock a challenge."""
        try:
            row = self._connection.execute(
                "SELECT challenge_id, customer_id, terms_digest, code_digest, created_at, expires_at, attempts, status "
                "FROM authentication_challenges WHERE challenge_id = %s FOR UPDATE",
                (challenge_id,),
            ).fetchone()
        except psycopg.errors.InvalidTextRepresentation:
            self._connection.rollback()
            return None
        if row is None:
            return None
        cid, customer, terms, code, created, expires, attempts, status = row
        return Challenge(str(cid), str(customer), terms, code, created, expires, attempts, ChallengeStatus(status))

    def save(self, challenge: Challenge) -> None:
        """Write the attempts and status."""
        self._connection.execute(
            "UPDATE authentication_challenges SET attempts = %s, status = %s WHERE challenge_id = %s",
            (challenge.attempts, challenge.status.value, challenge.challenge_id),
        )


class PostgresProofs:
    """`ProofRepository` over `authentication_proofs`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def add(self, proof: AuthenticationProof) -> None:
        """Store a proof."""
        self._connection.execute(
            "INSERT INTO authentication_proofs (proof_id, customer_id, terms_digest, expires_at, consumed) "
            "VALUES (%s, %s, %s, %s, %s)",
            (proof.proof_id, proof.customer_id, proof.terms_digest, proof.expires_at, proof.consumed),
        )

    def get_for_update(self, proof_id: str) -> AuthenticationProof | None:
        """Read and lock a proof."""
        row = self._connection.execute(
            "SELECT proof_id, customer_id, terms_digest, expires_at, consumed FROM authentication_proofs "
            "WHERE proof_id = %s FOR UPDATE",
            (proof_id,),
        ).fetchone()
        return None if row is None else AuthenticationProof(row[0], str(row[1]), row[2], row[3], row[4])

    def mark_consumed(self, proof_id: str) -> None:
        """A proof is good for one consent."""
        self._connection.execute("UPDATE authentication_proofs SET consumed = TRUE WHERE proof_id = %s", (proof_id,))


class PostgresConsents:
    """`ConsentRepository` over `consents`."""

    _COLUMNS = (
        "consent_id, customer_id, payer_account, payee_account, client_id, currency, max_amount_minor, scope, "
        "period_seconds, created_at, expires_at, revoked_at, used_at"
    )

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def add(self, consent: Consent) -> None:
        """Store a consent."""
        terms = consent.terms
        self._connection.execute(
            f"INSERT INTO consents ({self._COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                consent.consent_id,
                consent.customer_id,
                terms.payer_account,
                terms.payee_account,
                terms.client_id,
                terms.max_amount.currency,
                terms.max_amount.minor,
                terms.scope.value,
                None if terms.period is None else int(terms.period.total_seconds()),
                consent.created_at,
                consent.expires_at,
                consent.revoked_at,
                consent.used_at,
            ),
        )

    def get_for_update(self, consent_id: str) -> Consent | None:
        """Read and lock a consent, so a revocation and a payment under it run one after the other."""
        try:
            row = self._connection.execute(
                f"SELECT {self._COLUMNS} FROM consents WHERE consent_id = %s FOR UPDATE", (consent_id,)
            ).fetchone()
        except psycopg.errors.InvalidTextRepresentation:
            self._connection.rollback()
            return None
        if row is None:
            return None
        (cid, customer, payer, payee, client, currency, maximum, scope, period, created, expires, revoked, used) = row
        terms = ConsentTerms(
            payer,
            payee,
            client,
            Money(maximum, currency.strip()),
            ConsentScope(scope),
            None if period is None else timedelta(seconds=period),
        )
        return Consent(str(cid), str(customer), terms, created, expires, revoked, used)

    def save(self, consent: Consent) -> None:
        """Write the revocation and use times."""
        self._connection.execute(
            "UPDATE consents SET revoked_at = %s, used_at = %s WHERE consent_id = %s",
            (consent.revoked_at, consent.used_at, consent.consent_id),
        )

    def count(self) -> int:
        """How many consents exist."""
        row = self._connection.execute("SELECT count(*) FROM consents").fetchone()
        assert row is not None
        return int(row[0])

    def spent_since(self, consent_id: str, since: datetime, currency: str) -> Money:
        """What payments under this consent moved at or after `since`."""
        row = self._connection.execute(
            "SELECT COALESCE(sum(amount_minor), 0)::bigint FROM payments "
            "WHERE consent_id = %s AND created_at >= %s AND currency = %s",
            (consent_id, since, currency),
        ).fetchone()
        assert row is not None
        return Money(row[0], currency)


class PostgresTokens:
    """`TokenRepository` over `access_tokens` and `seen_proofs`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def add(self, token: AccessToken) -> None:
        """Store an issued token."""
        self._connection.execute(
            "INSERT INTO access_tokens (token_digest, client_id, key_thumbprint, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (token.token_digest, token.client_id, token.key_thumbprint, token.created_at, token.expires_at),
        )

    def get(self, token_digest: str) -> AccessToken | None:
        """Find a token by the digest of its value."""
        row = self._connection.execute(
            "SELECT token_digest, client_id, key_thumbprint, created_at, expires_at FROM access_tokens "
            "WHERE token_digest = %s",
            (token_digest,),
        ).fetchone()
        return None if row is None else AccessToken(row[0], row[1], row[2], row[3], row[4])

    def remember_proof(self, jti: str, at: datetime) -> bool:
        """Record a proof id; False when it was seen before (a replay)."""
        row = self._connection.execute(
            "INSERT INTO seen_proofs (jti, seen_at) VALUES (%s, %s) ON CONFLICT (jti) DO NOTHING RETURNING jti",
            (jti, at),
        ).fetchone()
        return row is not None


class PostgresPayments:
    """`PaymentRepository` over `payments`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def add(self, payment: PaymentRecord) -> None:
        """Store a payment."""
        self._connection.execute(
            "INSERT INTO payments (payment_id, consent_id, client_id, payer_account, payee_account, amount_minor, "
            "currency, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                payment.payment_id,
                payment.consent_id,
                payment.client_id,
                payment.payer_account,
                payment.payee_account,
                payment.amount.minor,
                payment.amount.currency,
                payment.created_at,
            ),
        )

    def get(self, payment_id: str, client_id: str) -> PaymentRecord | None:
        """Find a payment made for this client."""
        try:
            row = self._connection.execute(
                "SELECT payment_id, consent_id, client_id, payer_account, payee_account, amount_minor, currency, "
                "created_at FROM payments WHERE payment_id = %s AND client_id = %s",
                (payment_id, client_id),
            ).fetchone()
        except psycopg.errors.InvalidTextRepresentation:
            self._connection.rollback()
            return None
        if row is None:
            return None
        pid, consent, client, payer, payee, amount, currency, created = row
        return PaymentRecord(str(pid), str(consent), client, payer, payee, Money(amount, currency.strip()), created)


class PostgresLedger:
    """`LedgerRepository` over `journal_entries`, `postings` and the balances of `bank_accounts`."""

    def __init__(self, connection: psycopg.Connection[Any], new_id: Any) -> None:
        self._connection = connection
        self._new_id = new_id

    def post(self, transfer: Transfer, kind: str, reference: str, at: datetime) -> None:
        """Append the transfer's two postings and move the two balances; the deferred triggers check them together."""
        entry_id = self._new_id()
        currency = transfer.amount.currency
        with self._connection.pipeline():
            self._connection.execute(
                "INSERT INTO journal_entries (id, kind, reference, created_at) VALUES (%s, %s, %s, %s)",
                (entry_id, kind, reference, at),
            )
            for posting in transfer.postings():
                self._connection.execute(
                    "INSERT INTO postings (entry_id, account_id, amount_minor, currency, created_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (entry_id, posting.account_id, posting.amount.minor, currency, at),
                )
                self._connection.execute(
                    "UPDATE bank_accounts SET balance_minor = balance_minor + %s WHERE account_id = %s",
                    (posting.amount.minor, posting.account_id),
                )

    def total(self, currency: str) -> Money:
        """The sum of every posting in `currency`."""
        row = self._connection.execute(
            "SELECT COALESCE(sum(amount_minor), 0)::bigint FROM postings WHERE currency = %s", (currency,)
        ).fetchone()
        assert row is not None
        return Money(row[0], currency)


class PostgresIdempotencyKeys:
    """`IdempotencyStore` over `idempotency_keys`, scoped to the merchant.

    `claim` takes a transaction-scoped advisory lock on (merchant, key), so concurrent callers of one key queue up and
    each sees the previous caller's committed row. The primary key is the backstop if that ever fails.
    """

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def claim(self, client_id: str, key: IdempotencyKey, request_fingerprint: str, now: datetime) -> Claim:
        """Lock the key and say whether to do the work, replay the stored response, or refuse a reused key."""
        self._connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{client_id}\x1f{key.value}",)
        )
        row = self._connection.execute(
            "SELECT request_fingerprint, response_status, response_body, expires_at FROM idempotency_keys "
            "WHERE client_id = %s AND key = %s",
            (client_id, key.value),
        ).fetchone()
        if row is None:
            return Fresh()
        stored_fingerprint, status, body, expires_at = row
        if expires_at <= now:
            self._connection.execute(
                "DELETE FROM idempotency_keys WHERE client_id = %s AND key = %s", (client_id, key.value)
            )
            return Fresh()
        if stored_fingerprint != request_fingerprint:
            return Reused()
        return Replay(StoredResponse(status, body))

    def store(
        self,
        client_id: str,
        key: IdempotencyKey,
        request_fingerprint: str,
        response: StoredResponse,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        """Record the response under the key, in the transaction that made the effect."""
        self._connection.execute(
            "INSERT INTO idempotency_keys (client_id, key, request_fingerprint, response_status, response_body, "
            "created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (client_id, key.value, request_fingerprint, response.status, Jsonb(dict(response.body)), now, expires_at),
        )


@dataclass(slots=True)
class PostgresUnitOfWork:
    """The repositories of one connection, all inside the transaction the pool opened for it."""

    accounts: PostgresAccounts
    customers: PostgresCustomers
    clients: PostgresClients
    challenges: PostgresChallenges
    proofs: PostgresProofs
    consents: PostgresConsents
    tokens: PostgresTokens
    payments: PostgresPayments
    ledger: PostgresLedger
    keys: PostgresIdempotencyKeys


class PostgresUnitOfWorkFactory:
    """Open a transaction on a pooled connection: commit on a normal exit, roll back on an exception."""

    def __init__(self, pool: ConnectionPool[psycopg.Connection[Any]], new_id: Any) -> None:
        self._pool = pool
        self._new_id = new_id

    @contextmanager
    def __call__(self) -> Iterator[UnitOfWork]:
        """One unit of work."""
        with self._pool.connection() as connection:
            yield PostgresUnitOfWork(
                PostgresAccounts(connection),
                PostgresCustomers(connection),
                PostgresClients(connection),
                PostgresChallenges(connection),
                PostgresProofs(connection),
                PostgresConsents(connection),
                PostgresTokens(connection),
                PostgresPayments(connection),
                PostgresLedger(connection, self._new_id),
                PostgresIdempotencyKeys(connection),
            )


class PostgresHealth:
    """`HealthProbe`: can a pooled connection run `SELECT 1`?"""

    def __init__(self, pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
        self._pool = pool

    def check(self) -> None:
        """Return normally when healthy; raise when not."""
        with self._pool.connection(timeout=2.0) as connection:
            connection.execute("SELECT 1")
