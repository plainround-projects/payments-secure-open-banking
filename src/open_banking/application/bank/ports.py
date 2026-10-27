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


class Crypto(Protocol):
    """Every cryptographic operation the bank and the merchant need. The adapter wraps the libraries."""

    def hash_secret(self, secret: str) -> str:
        """A salted, slow hash for a PIN or a client secret."""
        ...

    def verify_secret(self, secret: str, stored: str) -> bool:
        """Compare in constant time. A malformed `stored` is simply False."""
        ...

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

    def thumbprint(self, jwk: Jwk) -> str:
        """The RFC 7638 thumbprint of a public key."""
        ...

    def verify_signature(self, public_jwk: Jwk, message: bytes, signature: str) -> bool:
        """True when `signature` (base64url) is the key's signature over `message`."""
        ...

    def verify_dpop_proof(
        self, proof: str, public_jwk: Jwk, *, method: str, url: str, now: datetime, access_token: str | None
    ) -> DpopClaims:
        """Check a proof of possession: signature, method, URL, freshness, and the access token's hash.

        Raises `ClientAuthenticationError` when any of them is wrong.
        """
        ...


@dataclass(frozen=True, slots=True)
class DpopClaims:
    """The parts of a verified proof of possession the bank keeps: its id (to refuse a replay) and its time."""

    jti: str
    issued_at: datetime


class OtpChannel(Protocol):
    """Delivers a one-time code to the customer's device. The bank never logs the code."""

    def send(self, customer_id: str, code: str, purpose: str) -> None:
        """Hand the code to the channel."""
        ...


@dataclass(frozen=True, slots=True)
class Account:
    """A bank account with its balance, which the database keeps equal to the sum of its postings."""

    account_id: str
    owner: str
    balance: Money
    vault: bool = False


@dataclass(frozen=True, slots=True)
class Customer:
    """A customer who can authenticate: a username, a PIN (stored as a slow hash) and the account they own."""

    customer_id: str
    username: str
    account_id: str
    pin_hash: str


@dataclass(frozen=True, slots=True)
class OAuthClient:
    """A registered merchant: its secret (stored as a slow hash) and the public key its requests are checked with."""

    client_id: str
    name: str
    secret_hash: str
    public_jwk: Jwk


@dataclass(frozen=True, slots=True)
class AccessToken:
    """An issued token as stored: the digest of the token, whose client, which key it is bound to, until when."""

    token_digest: str
    client_id: str
    key_thumbprint: str
    created_at: datetime
    expires_at: datetime


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


class AccountRepository(Protocol):
    """Accounts, locked while a payment moves money."""

    def get(self, account_id: str) -> Account | None:
        """Read an account."""
        ...

    def get_for_update(self, account_id: str) -> Account | None:
        """Read and lock the account until the unit of work ends."""
        ...


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


class ClientRepository(Protocol):
    """Registered merchants."""

    def get(self, client_id: str) -> OAuthClient | None:
        """Find a client."""
        ...

    def add(self, client: OAuthClient) -> None:
        """Register a client."""
        ...


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


class PaymentRepository(Protocol):
    """Payments."""

    def add(self, payment: PaymentRecord) -> None:
        """Store a payment."""
        ...

    def get(self, payment_id: str, client_id: str) -> PaymentRecord | None:
        """Find a payment made for this client."""
        ...


class LedgerRepository(Protocol):
    """The append-only ledger and the balances it keeps."""

    def post(self, transfer: Transfer, kind: str, reference: str, at: datetime) -> None:
        """Append the transfer's two postings and move the two balances in the same statement group."""
        ...

    def total(self, currency: str) -> Money:
        """The sum of every posting in `currency`; zero whenever the ledger is whole."""
        ...


@dataclass(frozen=True, slots=True)
class Fresh:
    """The key has not been seen: do the work."""


@dataclass(frozen=True, slots=True)
class Replay:
    """The key was seen with this same request: answer with the stored response."""

    response: StoredResponse


@dataclass(frozen=True, slots=True)
class Reused:
    """The key was seen with a different request."""


Claim = Fresh | Replay | Reused


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


UnitOfWorkFactory = Callable[[], AbstractContextManager[UnitOfWork]]


class HealthProbe(Protocol):
    """Reports whether a dependency is reachable."""

    def check(self) -> None:
        """Return normally when healthy; raise when not."""
        ...


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
