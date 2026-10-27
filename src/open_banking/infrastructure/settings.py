"""Configuration from the environment, validated once at startup (twelve-factor). Secrets only from here."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


class ConfigurationError(ValueError):
    """An environment variable is missing or has an unusable value."""


def read_text(environ: Mapping[str, str], name: str, default: str | None = None) -> str:
    """A required, non-empty setting, or the default."""
    value = environ.get(name, default if default is not None else "").strip()
    if not value:
        raise ConfigurationError(f"{name} must be set and not empty")
    return value


def read_positive_int(environ: Mapping[str, str], name: str, default: int, *, maximum: int | None = None) -> int:
    """A positive integer setting, at most `maximum` when given."""
    raw = environ.get(name, str(default)).strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ConfigurationError(f"{name} must be a positive integer, got {raw!r}")
    if maximum is not None and int(raw) > maximum:
        raise ConfigurationError(f"{name} must be at most {maximum}, got {raw}")
    return int(raw)


def read_url(environ: Mapping[str, str], name: str, default: str) -> str:
    """An http(s) URL setting without a trailing slash."""
    value = read_text(environ, name, default).rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ConfigurationError(f"{name} must start with http:// or https://, got {value!r}")
    return value


def read_level(environ: Mapping[str, str]) -> str:
    """LOG_LEVEL, upper-cased and checked."""
    level = read_text(environ, "LOG_LEVEL", "INFO").upper()
    if level not in _LEVELS:
        raise ConfigurationError(f"LOG_LEVEL must be one of {sorted(_LEVELS)}, got {level!r}")
    return level


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    """The PostgreSQL part of the configuration; the password is never printed."""

    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)  # never printed or logged
    connect_timeout_seconds: int
    pool_max_size: int


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """Where an HTTP API listens and how long it waits for in-flight requests when told to stop."""

    host: str
    port: int
    shutdown_grace_seconds: int


@dataclass(frozen=True, slots=True)
class MerchantConfig:
    """Everything the merchant process reads from the environment. It has no database and no bank secrets."""

    server: ServerSettings
    bank_url: str
    client_id: str
    client_secret: str = field(repr=False)  # never printed or logged
    private_key_file: Path
    payee_account: str
    request_timeout_seconds: float
    attempts: int
    retry_base_delay_ms: int
    log_level: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> MerchantConfig:
        """Read and validate; raises `ConfigurationError` naming the first bad variable."""
        env = os.environ if environ is None else environ
        return cls(
            server=ServerSettings(
                read_text(env, "MERCHANT_HOST", "127.0.0.1"),
                read_positive_int(env, "MERCHANT_PORT", 8464, maximum=65535),
                read_positive_int(env, "SHUTDOWN_GRACE_SECONDS", 10),
            ),
            bank_url=read_url(env, "BANK_URL", "http://127.0.0.1:8463"),
            client_id=read_text(env, "MERCHANT_CLIENT_ID", "merchant-1"),
            client_secret=read_text(env, "MERCHANT_CLIENT_SECRET"),
            private_key_file=Path(read_text(env, "MERCHANT_PRIVATE_KEY_FILE", "var/merchant-1-key.pem")),
            payee_account=read_text(env, "MERCHANT_PAYEE_ACCOUNT", "acct-shop"),
            request_timeout_seconds=float(read_positive_int(env, "BANK_TIMEOUT_SECONDS", 5)),
            attempts=read_positive_int(env, "BANK_RETRY_ATTEMPTS", 3, maximum=10),
            retry_base_delay_ms=read_positive_int(env, "BANK_RETRY_BASE_DELAY_MS", 50),
            log_level=read_level(env),
        )
