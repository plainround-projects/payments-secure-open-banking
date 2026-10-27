"""Access for merchants: client credentials exchanged for a token bound to the merchant's own key (DPoP)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from open_banking.application.bank.ports import AccessToken, BankSettings, Crypto, UnitOfWorkFactory
from open_banking.application.logs import fields
from open_banking.application.ports import Clock
from open_banking.domain.errors import ClientAuthenticationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """An access token, shown once, and how long it lasts."""

    access_token: str
    expires_in_seconds: int


class IssueToken:
    """Authenticate the merchant (secret and proof of possession of its key) and issue a key-bound token."""

    def __init__(self, uow_factory: UnitOfWorkFactory, crypto: Crypto, clock: Clock, settings: BankSettings) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._clock = clock
        self._settings = settings
        self._decoy = crypto.hash_secret("not-a-client-secret")

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


class AuthenticateCaller:
    """Check a request from a merchant: a valid, unexpired token and a fresh proof that the caller holds its key."""

    def __init__(self, uow_factory: UnitOfWorkFactory, crypto: Crypto, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._clock = clock

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
