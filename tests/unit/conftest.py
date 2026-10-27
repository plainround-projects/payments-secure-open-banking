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

BANK_URL = "http://bank.test"


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


@dataclass
class Merchant:
    """A registered merchant as its own process would hold it: id, secret, key pair."""

    client_id: str
    secret: str
    private_pem: str
    public_jwk: dict[str, Any]


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


def _register(bank: BankContainer, client_id: str) -> Merchant:
    pem, jwk = keys.generate_keypair()
    secret = f"secret-of-{client_id}-0123456789"
    bank.register_client.execute(client_id, client_id, secret, jwk)
    return Merchant(client_id, secret, pem, jwk)


@pytest.fixture
def world() -> World:
    return build_world()
