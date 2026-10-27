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


@pytest.fixture(autouse=True)
def _schema(settings: BankConfig) -> None:
    """Every test starts from a freshly loaded schema and seed, so no test sees another's payments."""
    wait_until_ready(settings)
    with psycopg.connect(postgres.conninfo(settings.postgres), autocommit=True) as admin:
        load_schema(admin)


@pytest.fixture
def connection(settings: BankConfig) -> Iterator[psycopg.Connection[Any]]:
    """A plain connection (autocommit off) for tests that write SQL directly."""
    with psycopg.connect(postgres.conninfo(settings.postgres)) as connection:
        yield connection


class DbView:
    """What the shared test helpers ask of a database: find a customer by name."""

    def __init__(self, bank: BankContainer) -> None:
        self._bank = bank

    def customers_by_name(self, username: str) -> Customer:
        with self._bank.uow_factory() as uow:
            customer = uow.customers.by_username(username)
        assert customer is not None
        return customer


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


def _register(bank: BankContainer, client_id: str) -> Merchant:
    pem, jwk = keys.generate_keypair()
    secret = f"secret-of-{client_id}-0123456789"
    bank.register_client.execute(client_id, client_id, secret, jwk)
    return Merchant(client_id, secret, pem, jwk)


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
