"""Key pairs, signatures and proofs of possession, as the merchant (and the bank, for its results) make them."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

_ALGORITHM = ECAlgorithm(ECAlgorithm.SHA256)


def b64url(data: bytes) -> str:
    """Base64url without padding, as JOSE writes it."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    """The inverse of `b64url`."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def generate_keypair() -> tuple[str, dict[str, Any]]:
    """A new P-256 key pair: the private key as PEM, the public key as a JWK."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, public_jwk_of(pem)


def public_jwk_of(private_pem: str) -> dict[str, Any]:
    """The public half of a private key, as a JWK with only the members a thumbprint uses."""
    private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    assert isinstance(private_key, ec.EllipticCurvePrivateKey)
    jwk = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
    return {key: jwk[key] for key in ("kty", "crv", "x", "y")}


def read_private_key(path: Path) -> str:
    """Read a PEM private key file."""
    return path.read_text(encoding="utf-8")


def write_private_key(path: Path, pem: str) -> None:
    """Write a private key readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(pem, encoding="utf-8")


def sign(private_pem: str, message: bytes) -> str:
    """An ES256 signature (raw R||S, base64url) over `message`."""
    key = _ALGORITHM.prepare_key(private_pem)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    return b64url(_ALGORITHM.sign(message, key))


def verify(public_jwk: dict[str, Any], message: bytes, signature: str) -> bool:
    """True when `signature` is the key's ES256 signature over `message`."""
    try:
        key = ECAlgorithm.from_jwk(json.dumps(public_jwk))
        return bool(_ALGORITHM.verify(message, key, b64url_decode(signature)))
    except (ValueError, TypeError, jwt.PyJWTError):
        return False


def thumbprint(jwk: dict[str, Any]) -> str:
    """The RFC 7638 thumbprint of an EC public key."""
    members = {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]}
    return b64url(hashlib.sha256(json.dumps(members, sort_keys=True, separators=(",", ":")).encode()).digest())


def normalize_url(url: str) -> str:
    """The URL a proof is bound to: scheme, host and path; no query and no fragment."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def access_token_hash(access_token: str) -> str:
    """The `ath` member of a proof: the base64url SHA-256 of the access token it accompanies."""
    return b64url(hashlib.sha256(access_token.encode()).digest())


def build_dpop_proof(
    private_pem: str,
    public_jwk: dict[str, Any],
    *,
    method: str,
    url: str,
    now: datetime,
    access_token: str | None = None,
) -> str:
    """A proof of possession of the key for one request: a signed JWT naming the method, the URL, a fresh id."""
    claims: dict[str, Any] = {
        "jti": uuid.uuid4().hex,
        "htm": method.upper(),
        "htu": normalize_url(url),
        "iat": int(now.timestamp()),
    }
    if access_token is not None:
        claims["ath"] = access_token_hash(access_token)
    encoded: str = jwt.encode(claims, private_pem, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": public_jwk})
    return encoded
