"""The cryptographic operations of the bank, behind the `Crypto` port."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from open_banking.application.bank.ports import DpopClaims, Jwk
from open_banking.domain.errors import ClientAuthenticationError
from open_banking.infrastructure import keys


class StandardCrypto:
    """Slow salted hashes for secrets, keyed digests for codes and tokens, and the checks on signed requests.

    `code_source` makes the one-time code replaceable in tests; production draws it from the system's secure source.
    """

    def __init__(
        self,
        *,
        pepper: str,
        scrypt_n: int = 2**14,
        dpop_max_skew: timedelta = timedelta(seconds=60),
        code_source: Callable[[], str] | None = None,
    ) -> None:
        self._pepper = pepper.encode()
        self._scrypt_n = scrypt_n
        self._max_skew = dpop_max_skew
        self._code_source = code_source or (lambda: f"{secrets.randbelow(10**6):06d}")

    def hash_secret(self, secret: str) -> str:
        """`scrypt$n$r$p$salt$hash`, with a fresh random salt."""
        salt = os.urandom(16)
        digest = hashlib.scrypt(secret.encode(), salt=salt, n=self._scrypt_n, r=8, p=1, dklen=32)
        return f"scrypt${self._scrypt_n}$8$1${keys.b64url(salt)}${keys.b64url(digest)}"

    def verify_secret(self, secret: str, stored: str) -> bool:
        """Compare in constant time; the cost parameters come from the stored value, so they can change over time."""
        try:
            scheme, n, r, p, salt, expected = stored.split("$")
            if scheme != "scrypt":
                return False
            digest = hashlib.scrypt(
                secret.encode(), salt=keys.b64url_decode(salt), n=int(n), r=int(r), p=int(p), dklen=32
            )
            return hmac.compare_digest(digest, keys.b64url_decode(expected))
        except (ValueError, TypeError):
            return False

    def new_code(self) -> str:
        """A six-digit one-time code."""
        return self._code_source()

    def code_digest(self, challenge_id: str, code: str) -> str:
        """HMAC-SHA256 under the pepper, tied to the challenge, so a stolen digest cannot be tried offline."""
        return hmac.new(self._pepper, f"code:{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()

    def new_token(self) -> str:
        """An unguessable bearer value."""
        return secrets.token_urlsafe(32)

    def token_digest(self, token: str) -> str:
        """The keyed digest under which a bearer value is stored."""
        return hmac.new(self._pepper, f"token:{token}".encode(), hashlib.sha256).hexdigest()

    def thumbprint(self, jwk: Jwk) -> str:
        """The RFC 7638 thumbprint of a public key."""
        return keys.thumbprint(dict(jwk))

    def verify_signature(self, public_jwk: Jwk, message: bytes, signature: str) -> bool:
        """True when `signature` is the key's signature over `message`."""
        return keys.verify(dict(public_jwk), message, signature)

    def verify_dpop_proof(
        self, proof: str, public_jwk: Jwk, *, method: str, url: str, now: datetime, access_token: str | None
    ) -> DpopClaims:
        """Check signature, type, method, URL, freshness and (with a token) the token hash of a proof of possession."""
        try:
            header = jwt.get_unverified_header(proof)
            if header.get("typ") != "dpop+jwt" or header.get("alg") != "ES256":
                raise ClientAuthenticationError("invalid merchant credential")
            embedded = header.get("jwk")
            if embedded is not None and self.thumbprint(embedded) != self.thumbprint(public_jwk):
                raise ClientAuthenticationError("the proof was signed with a different key")
            key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(dict(public_jwk)))
            assert isinstance(key, ec.EllipticCurvePublicKey)
            claims: dict[str, Any] = jwt.decode(
                proof,
                key,
                algorithms=["ES256"],
                options={"verify_exp": False, "verify_nbf": False, "verify_iat": False, "verify_aud": False},
            )
        except (jwt.PyJWTError, ValueError, KeyError, TypeError) as error:
            raise ClientAuthenticationError("invalid merchant credential") from error
        if claims.get("htm") != method.upper() or claims.get("htu") != keys.normalize_url(url):
            raise ClientAuthenticationError("the proof was made for a different request")
        issued_at = _timestamp(claims.get("iat"))
        if abs(now - issued_at) > self._max_skew:
            raise ClientAuthenticationError("the proof is too old or from the future")
        if access_token is not None and claims.get("ath") != keys.access_token_hash(access_token):
            raise ClientAuthenticationError("the proof does not belong to this access token")
        if access_token is None and "ath" in claims:
            raise ClientAuthenticationError("the proof names an access token the request does not carry")
        jti = claims.get("jti")
        if not isinstance(jti, str) or not jti:
            raise ClientAuthenticationError("invalid merchant credential")
        return DpopClaims(jti, issued_at)


def _timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClientAuthenticationError("invalid merchant credential")
    return datetime.fromtimestamp(value, tz=UTC)
