"""The bank process's configuration, read from the environment and validated once at startup."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from open_banking.application.bank.ports import BankSettings
from open_banking.infrastructure.settings import (
    ConfigurationError,
    PostgresSettings,
    ServerSettings,
    read_level,
    read_positive_int,
    read_text,
    read_url,
)


@dataclass(frozen=True, slots=True)
class BankConfig:
    """Everything the bank process reads from the environment. See .env.example for each variable."""

    postgres: PostgresSettings
    server: ServerSettings
    public_url: str
    timings: BankSettings
    pepper: str = field(repr=False)  # keys the digests of one-time codes and bearer values
    signing_key_file: Path
    otp_outbox_file: Path
    scrypt_n: int
    log_level: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> BankConfig:
        """Read and validate; raises `ConfigurationError` naming the first bad variable."""
        env = os.environ if environ is None else environ
        pepper = read_text(env, "BANK_PEPPER")
        if len(pepper) < 16:
            raise ConfigurationError("BANK_PEPPER must be at least 16 characters")
        port = read_positive_int(env, "BANK_PORT", 8463, maximum=65535)
        host = read_text(env, "BANK_HOST", "127.0.0.1")
        return cls(
            postgres=PostgresSettings(
                host=read_text(env, "POSTGRES_HOST", "localhost"),
                port=read_positive_int(env, "POSTGRES_PORT", 54613, maximum=65535),
                database=read_text(env, "POSTGRES_DB", "openbanking"),
                user=read_text(env, "POSTGRES_USER", "openbanking"),
                password=read_text(env, "POSTGRES_PASSWORD"),
                connect_timeout_seconds=read_positive_int(env, "POSTGRES_CONNECT_TIMEOUT_SECONDS", 5),
                pool_max_size=read_positive_int(env, "DB_POOL_MAX_SIZE", 20),
            ),
            server=ServerSettings(host, port, read_positive_int(env, "SHUTDOWN_GRACE_SECONDS", 10)),
            public_url=read_url(env, "BANK_PUBLIC_URL", f"http://{host}:{port}"),
            timings=BankSettings(
                challenge_time_to_live=timedelta(minutes=read_positive_int(env, "CHALLENGE_MINUTES", 5)),
                proof_time_to_live=timedelta(minutes=read_positive_int(env, "PROOF_MINUTES", 2)),
                token_time_to_live=timedelta(minutes=read_positive_int(env, "TOKEN_MINUTES", 10)),
                consent_time_to_live=timedelta(minutes=read_positive_int(env, "CONSENT_MINUTES", 10)),
                recurring_consent_time_to_live=timedelta(days=read_positive_int(env, "RECURRING_CONSENT_DAYS", 90)),
                dpop_max_skew=timedelta(seconds=read_positive_int(env, "DPOP_MAX_SKEW_SECONDS", 60)),
                idempotency_key_time_to_live=timedelta(hours=read_positive_int(env, "IDEMPOTENCY_KEY_TTL_HOURS", 24)),
            ),
            pepper=pepper,
            signing_key_file=Path(read_text(env, "BANK_SIGNING_KEY_FILE", "var/bank-signing-key.pem")),
            otp_outbox_file=Path(read_text(env, "OTP_OUTBOX_FILE", "var/otp-outbox.log")),
            scrypt_n=_scrypt_n(env),
            log_level=read_level(env),
        )


def _scrypt_n(env: Mapping[str, str]) -> int:
    n = read_positive_int(env, "SCRYPT_N", 2**14)
    if n < 2 or n & (n - 1):
        raise ConfigurationError(f"SCRYPT_N must be a power of two, got {n}")
    return n
