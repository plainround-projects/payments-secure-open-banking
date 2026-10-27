"""The bank's composition root: its adapters wired into its use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from open_banking.application.bank.access import AuthenticateCaller, IssueToken
from open_banking.application.bank.authentication import StartChallenge, VerifyChallenge
from open_banking.application.bank.consents import GrantConsent, RevokeConsent
from open_banking.application.bank.enrollment import EnrollCustomer, RegisterClient
from open_banking.application.bank.idempotent import IdempotentExecutor
from open_banking.application.bank.payments import GetPayment, InitiatePayment
from open_banking.application.bank.ports import BankSettings, Crypto, HealthProbe, OtpChannel, UnitOfWorkFactory
from open_banking.application.bank.queries import CheckHealth, GetBalances
from open_banking.application.ports import Clock, IdSource
from open_banking.infrastructure import keys, postgres
from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.crypto import StandardCrypto
from open_banking.infrastructure.otp import FileOtpChannel
from open_banking.infrastructure.system import RandomIds, SystemClock


@dataclass(frozen=True, slots=True)
class BankContainer:
    """The bank's use cases, the key it signs results with, and how to release resources."""

    start_challenge: StartChallenge
    verify_challenge: VerifyChallenge
    grant_consent: GrantConsent
    revoke_consent: RevokeConsent
    issue_token: IssueToken
    authenticate: AuthenticateCaller
    initiate_payment: InitiatePayment
    get_payment: GetPayment
    balances: GetBalances
    enroll_customer: EnrollCustomer
    register_client: RegisterClient
    health: CheckHealth
    crypto: Crypto
    public_url: str
    signing_pem: str
    signing_jwk: dict[str, Any]
    otp: OtpChannel
    uow_factory: UnitOfWorkFactory
    close: Callable[[], None]


def assemble_bank(
    *,
    uow_factory: UnitOfWorkFactory,
    crypto: Crypto,
    otp: OtpChannel,
    clock: Clock,
    ids: IdSource,
    settings: BankSettings,
    public_url: str,
    signing_pem: str,
    probes: dict[str, HealthProbe],
    close: Callable[[], None] = lambda: None,
) -> BankContainer:
    """Wire the bank's use cases around the given ports. Tests call this with in-memory ones."""
    executor = IdempotentExecutor(uow_factory, clock, settings.idempotency_key_time_to_live)
    return BankContainer(
        start_challenge=StartChallenge(uow_factory, crypto, otp, clock, ids, settings),
        verify_challenge=VerifyChallenge(uow_factory, crypto, clock, settings),
        grant_consent=GrantConsent(uow_factory, crypto, clock, ids, settings),
        revoke_consent=RevokeConsent(uow_factory, crypto, clock),
        issue_token=IssueToken(uow_factory, crypto, clock, settings),
        authenticate=AuthenticateCaller(uow_factory, crypto, clock),
        initiate_payment=InitiatePayment(executor, uow_factory, crypto, clock, ids),
        get_payment=GetPayment(uow_factory),
        balances=GetBalances(uow_factory),
        enroll_customer=EnrollCustomer(uow_factory, crypto, ids),
        register_client=RegisterClient(uow_factory, crypto),
        health=CheckHealth(probes),
        crypto=crypto,
        public_url=public_url.rstrip("/"),
        signing_pem=signing_pem,
        signing_jwk=keys.public_jwk_of(signing_pem),
        otp=otp,
        uow_factory=uow_factory,
        close=close,
    )


def load_or_create_signing_key(path: Path) -> str:
    """The bank's result-signing key: read it, or create it (readable by its owner only) on first start."""
    if path.exists():
        return keys.read_private_key(path)
    pem, _ = keys.generate_keypair()
    keys.write_private_key(path, pem)
    return pem


def build_bank_container(config: BankConfig, *, clock: Clock | None = None) -> BankContainer:
    """The real wiring: a PostgreSQL pool, the file-based code channel, and the standard cryptography."""
    pool = postgres.open_pool(config.postgres)
    ids = RandomIds()

    def new_id() -> UUID:
        return ids.new_id()

    return assemble_bank(
        uow_factory=postgres.PostgresUnitOfWorkFactory(pool, new_id),
        crypto=StandardCrypto(
            pepper=config.pepper, scrypt_n=config.scrypt_n, dpop_max_skew=config.timings.dpop_max_skew
        ),
        otp=FileOtpChannel(config.otp_outbox_file),
        clock=clock or SystemClock(),
        ids=ids,
        settings=config.timings,
        public_url=config.public_url,
        signing_pem=load_or_create_signing_key(config.signing_key_file),
        probes={"database": postgres.PostgresHealth(pool)},
        close=pool.close,
    )
