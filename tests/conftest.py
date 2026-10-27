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


@pytest.fixture(scope="session")
def settings() -> BankConfig:
    """From the environment (make loads .env; CI sets the variables); the password has no default."""
    return BankConfig.from_env()


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


def load_schema(connection: psycopg.Connection[Any]) -> None:
    """Drop everything and apply 001_schema.sql and 002_seed.sql: the same as `make db`."""
    for script in ("reset.sql", "001_schema.sql", "002_seed.sql"):
        connection.execute((SQL / script).read_text())


@pytest.fixture(scope="session")
def database(settings: BankConfig) -> None:
    """A fresh schema with the seed, once per test session. This drops whatever the database holds."""
    wait_until_ready(settings)
    with psycopg.connect(conninfo(settings.postgres), autocommit=True) as connection:
        load_schema(connection)
