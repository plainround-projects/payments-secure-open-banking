# Python walkthrough: the infrastructure package, line by line

## What it is

This note reads every line of the `infrastructure` package: the cryptography (scrypt hashes, keyed digests, ES256 signatures, thumbprints, DPoP proofs), key handling, the one-time-code channel, scripted network faults, settings for the bank and the merchant, logging, retry, the clock, the merchant's HTTP client for the bank, and the PostgreSQL pool, unit of work and repositories.

An **adapter** is code that connects the program to something outside it. Every adapter here implements a port that the application layer defined.

## Why this repository uses it

Everything that touches the outside world lives in one layer, so the rules above it stay testable without a database or a network. The security of the lesson is made concrete here: secrets are never stored (slow salted hashes and keyed digests only), tokens are bound to a key, every signature and proof is checked with a library designed for it, and the merchant's client keeps one idempotency key across every retry.

## Where it appears

| File | What it is |
| --- | --- |
| `src/open_banking/infrastructure/__init__.py` | The infrastructure package. |
| `src/open_banking/infrastructure/system.py` | The real clock, ids and the demo's offset clock. |
| `src/open_banking/infrastructure/retry.py` | Retry with exponential backoff and jitter. |
| `src/open_banking/infrastructure/logging.py` | Structured log lines with card masking. |
| `src/open_banking/infrastructure/keys.py` | Key pairs, signatures, thumbprints and DPoP proofs. |
| `src/open_banking/infrastructure/crypto.py` | The `Crypto` port's implementation. |
| `src/open_banking/infrastructure/otp.py` | The one-time-code channels. |
| `src/open_banking/infrastructure/faults.py` | Scripted network faults. |
| `src/open_banking/infrastructure/settings.py` | Shared settings readers and the merchant's configuration. |
| `src/open_banking/infrastructure/bank_settings.py` | The bank's configuration. |
| `src/open_banking/infrastructure/bank_client.py` | The merchant's HTTP client for the bank. |
| `src/open_banking/infrastructure/postgres.py` | The PostgreSQL adapters. |

## How it works underneath

**Hashing** turns text into a fixed-size fingerprint that cannot be reversed. **scrypt** is a slow, memory-hungry hash for passwords; a **salt** makes equal secrets hash differently; a **pepper** is a secret key kept outside the database. **HMAC** is a hash keyed with a secret. **Elliptic-curve signatures (ES256)** let the holder of a private key sign a message that anyone with the public key can check. A **JWK** is a key written as JSON; its **thumbprint** is a standard fingerprint. **DPoP** binds a token to a key by requiring a freshly signed proof with every request. **Advisory locks** let code take a lock on a number derived from a text so related work takes turns. **Pipeline mode** sends several SQL statements before waiting for answers.

## Walkthrough of this repository's use



### `src/open_banking/infrastructure/__init__.py` (1 lines)

```python
"""Adapters: PostgreSQL, cryptography, the one-time-code channel, the bank client, settings, logging."""
```

The docstring of the `infrastructure` package. An **adapter** is a piece of code that connects the program to something outside it: PostgreSQL, the cryptography library, the one-time-code channel, the HTTP client the merchant uses to reach the bank, the settings, and the log output.

### `src/open_banking/infrastructure/system.py` (39 lines)

```python
"""The real clock and the real id source."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID
```

The real clock and the real id source. `uuid` is the standard library module for universally unique identifiers (128-bit labels that will not collide in practice); `UTC` is the zero-offset time zone.

```python
class SystemClock:
    """The current UTC time."""

    def now(self) -> datetime:
        """Now, timezone-aware."""
        return datetime.now(UTC)
```

`SystemClock.now()` returns `datetime.now(UTC)`: the current time as a **timezone-aware** value (it knows it is UTC). A "naive" datetime has no zone and is ambiguous, so every instant in this program is aware.

```python
class RandomIds:
    """Random version-4 UUIDs."""

    def new_id(self) -> UUID:
        """A fresh UUID."""
        return uuid.uuid4()
```

`RandomIds.new_id()` returns `uuid.uuid4()`, a random version-4 UUID. Because ids come from an injected source, tests can use a predictable one.

```python
class OffsetClock:
    """The real clock plus an offset that only moves forward, so a demo can let a consent expire without waiting."""

    def __init__(self, base: SystemClock | None = None) -> None:
        self._base = base or SystemClock()
        self._offset = timedelta(0)

    def advance(self, delta: timedelta) -> None:
        """Move this clock ahead."""
        self._offset += delta

    def now(self) -> datetime:
        """Real time plus the offset."""
        return self._base.now() + self._offset
```

The real clock plus an offset that only moves forward, so a demo can let a consent expire without waiting. The constructor takes an optional base clock (`base or SystemClock()` uses the default when none is given) and starts the offset at `timedelta(0)`. `advance(delta)` adds to the offset (`+=`). `now()` returns the base time plus the offset.

### `src/open_banking/infrastructure/retry.py` (38 lines)

```python
"""Retry with exponential backoff and jitter for calls that cross the network."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
```

A generic helper that calls a function again after a failure, waiting longer each time. The imports: `random` (for the jitter, below), `time` (for `sleep`) and `Callable`.

```python
def retry[T](
    operation: Callable[[], T],
    *,
    attempts: int,
    base_delay_seconds: float,
    retry_on: tuple[type[BaseException], ...],
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    max_delay_seconds: float = 10.0,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Call `operation` up to `attempts` times, doubling the pause after each failure in `retry_on`.

    The pause is `base * 2**(attempt - 1)` capped at `max_delay_seconds`, then scaled by a random
    factor in [0.5, 1.0] so many clients do not retry in step. The last failure is raised.
    """
```

`def retry[T](...)` declares a function with a **type variable** `T` (Python 3.12 syntax). It says: whatever type `operation` returns, `retry` returns the same type, so the checker can follow the value through. The bare `*` makes every later parameter keyword-only. The parameters: `attempts` (the maximum number of calls), `base_delay_seconds` (the first pause), `retry_on` (a tuple of the error types worth retrying), `sleep` (the function that waits, replaceable in tests so they do not really wait), `rng` (a random number generator, replaceable so tests are repeatable), `max_delay_seconds` (a cap on any pause) and `on_retry` (an optional function told about each retry, used for logging).

```python
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    jitter = rng or random.Random()
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except retry_on as error:
            if attempt == attempts:
                raise
            if on_retry:
                on_retry(attempt, error)
            sleep(min(max_delay_seconds, base_delay_seconds * 2 ** (attempt - 1)) * jitter.uniform(0.5, 1.0))
    raise AssertionError("unreachable")  # pragma: no cover
```

The body. `attempts < 1` is refused with `ValueError` because "call at most zero times" makes no sense. `jitter = rng or random.Random()` uses the supplied generator or makes a fresh one. `for attempt in range(1, attempts + 1):` loops attempt numbers 1, 2, ... up to `attempts`. Inside, `try: return operation()` calls the function and returns its result on success. `except retry_on as error:` catches only the listed error types; any other error flies out untouched. If this was the last attempt, `raise` (alone) re-raises the same error to the caller. Otherwise `on_retry` is told, and the code sleeps for `min(max_delay_seconds, base_delay_seconds * 2 ** (attempt - 1)) * jitter.uniform(0.5, 1.0)`: the base delay doubles with every attempt (that is **exponential backoff**, `2 ** n` is two to the power n), is capped, and is then multiplied by a random factor between 0.5 and 1.0 (**jitter**), so many clients that failed together do not all retry at the same instant. The final `raise AssertionError("unreachable")` is a guard for the type checker; the `# pragma: no cover` comment tells the coverage tool not to count it.

### `src/open_banking/infrastructure/logging.py` (55 lines)

```python
"""Structured logging: one logfmt line per event, with anything shaped like a card number masked."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

# 13 to 19 digits, optionally grouped by spaces or dashes: the shape of a card number (a published test number too).
```

Structured logging. The docstring: one **logfmt** line per event (a line of `key=value` pairs separated by spaces), with anything shaped like a card number masked. Imports: `json` (used to quote values), `logging`, `re`, `sys`, `datetime`/`UTC` and typing helpers.

```python
_CARD_NUMBER = re.compile(r"(?<![\w.])(?:\d[ -]?){13,19}(?![\w])")
_SECRET_FIELDS = {"card_token", "card_number", "cvv", "password", "authorization"}
```

A **regular expression** that matches the shape of a card number: 13 to 19 digits, optionally separated by single spaces or dashes. The pieces: `(?<![\w.])` means "not preceded by a letter, digit, underscore or dot" (a **lookbehind**, so digits inside a longer word or a price such as `12.3456789012345` are not matched); `(?:\d[ -]?)` is one digit optionally followed by a space or dash (the `?:` makes a group that is not captured); `{13,19}` repeats it 13 to 19 times; `(?![\w])` means "not followed by a word character" (a **lookahead**). The comment notes that a published test card number would match too. `_SECRET_FIELDS` lists field names whose values are always withheld.

```python
def redact(text: str) -> str:
    """Mask anything that looks like a card number. Defense in depth: the code never logs card data to begin with."""
    return _CARD_NUMBER.sub("[redacted-number]", text)
```

`redact` replaces every card-number-shaped run of digits in `text` with `[redacted-number]` using `_CARD_NUMBER.sub(...)`. The docstring calls it defense in depth: the code never logs card data to begin with.

```python
def _quote(value: object) -> str:
    text = redact(str(value))
    return json.dumps(text) if not text or any(char in text for char in ' ="\n') else text
```

`_quote` prepares a value for a logfmt line. It converts the value to text and redacts it. If the text is empty or contains a space, an equals sign, a double quote or a newline (`any(char in text for char in ' ="\n')` is a **generator expression** inside `any`), it is wrapped in double quotes by `json.dumps(text)`, which also escapes quotes and newlines. Otherwise it is returned as it is. This makes every line splittable into `key=value` pairs without ambiguity.

```python
class LogfmtFormatter(logging.Formatter):
    """Format a log record as one logfmt line, with the record's extra fields as key=value pairs."""
```

A **formatter** decides what a log record looks like as text. This one subclasses the standard `logging.Formatter` and overrides `format`.

```python
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")
        parts = [
            f"ts={timestamp}",
            f"level={record.levelname}",
            f"logger={record.name}",
            f"msg={_quote(record.getMessage())}",
        ]
        extra: dict[str, Any] = getattr(record, "fields", {})
        parts.extend(
            f"{key}={'[withheld]' if key in _SECRET_FIELDS else _quote(value)}" for key, value in extra.items()
        )
        if record.exc_info:
            parts.append(f"exc={_quote(self.formatException(record.exc_info))}")
        return " ".join(parts)
```

`record.created` is the time the event happened as a number of seconds since 1970; `datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")` turns it into text such as `2026-10-27T12:00:00.123+00:00`. The line starts with four pairs: `ts`, `level` (such as `INFO`), `logger` (the name of the module) and `msg` (`record.getMessage()`, quoted). Then the extra fields: `getattr(record, "fields", {})` fetches the dictionary the `fields(...)` helper attached (or an empty one). Each pair is written `key=value`; if the key is in `_SECRET_FIELDS` the value is the fixed text `[withheld]`. If the record carries an exception (`record.exc_info`), the traceback text is added as `exc=...`. The parts are joined with single spaces.

```python
def configure_logging(level: str, stream: TextIO | None = None) -> None:
    """Send every log record to `stream` (stderr by default) as logfmt. Safe to call twice."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(LogfmtFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("uvicorn.access", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(max(logging.getLevelName(level), logging.WARNING))
```

`configure_logging` sets up logging for the whole program. It creates a `StreamHandler` writing to the given stream (standard error by default, `sys.stderr`), gives it the formatter, and makes it the **only** handler of the **root logger** (the ancestor of all loggers): `root.handlers[:] = [handler]` replaces the list contents, so calling the function twice does not print every line twice. `root.setLevel(level)` sets the minimum level that is shown (DEBUG, INFO, WARNING or ERROR). The last loop quiets two noisy libraries (`uvicorn.access` and `psycopg.pool`) to at least WARNING.

### `src/open_banking/infrastructure/keys.py` (114 lines)

```python
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
```

Key pairs, signatures and proofs of possession, as the merchant (and the bank, for its results) make them. Imports: `base64` (a text encoding for bytes), `hashlib` (SHA-256), `json`, `uuid`, `Path`, `urlsplit` and `urlunsplit` (split and rebuild a URL), `jwt` (the PyJWT library), `serialization` and `ec` (the `cryptography` library's key formats and elliptic-curve keys) and `ECAlgorithm` (PyJWT's elliptic-curve algorithm helper).

```python
_ALGORITHM = ECAlgorithm(ECAlgorithm.SHA256)
```

One shared helper object for the algorithm **ES256**: ECDSA (a signature scheme based on elliptic curves) with the curve P-256 and the hash SHA-256. Using one algorithm everywhere means a token cannot trick the bank into a weaker one.

```python
def b64url(data: bytes) -> str:
    """Base64url without padding, as JOSE writes it."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
```

**Base64url** writes bytes using only letters, digits, `-` and `_`, so the result is safe inside a URL, a header or JSON. `urlsafe_b64encode` encodes, `.rstrip(b"=")` removes the `=` padding characters as the JOSE standards do, and `.decode()` turns bytes into text.

```python
def b64url_decode(text: str) -> bytes:
    """The inverse of `b64url`."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
```

The inverse. Base64 needs the length to be a multiple of 4, so the padding that was removed is added back: `-len(text) % 4` is the number of missing `=` characters (a negative number modulo 4 gives the remainder to the next multiple).

```python
def generate_keypair() -> tuple[str, dict[str, Any]]:
    """A new P-256 key pair: the private key as PEM, the public key as a JWK."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, public_jwk_of(pem)
```

A new P-256 key pair. `ec.generate_private_key(ec.SECP256R1())` makes the private key (`SECP256R1` is P-256's other name). `private_bytes` writes it as text in the **PEM** format (the familiar `-----BEGIN PRIVATE KEY-----` block) using the **PKCS8** layout with no password. The function returns the PEM and the public key as a JWK.

```python
def public_jwk_of(private_pem: str) -> dict[str, Any]:
    """The public half of a private key, as a JWK with only the members a thumbprint uses."""
    private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    assert isinstance(private_key, ec.EllipticCurvePrivateKey)
    jwk = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
    return {key: jwk[key] for key in ("kty", "crv", "x", "y")}
```

The public half of a private key, as a **JWK** (JSON Web Key: a key described as a JSON object). `load_pem_private_key` reads the PEM; `assert isinstance(...)` tells the type checker it is an elliptic-curve key; `ECAlgorithm.to_jwk` writes the public key as JSON text, and the dictionary comprehension keeps only the four members a thumbprint uses: `kty` (key type), `crv` (curve), `x` and `y` (the point's coordinates).

```python
def read_private_key(path: Path) -> str:
    """Read a PEM private key file."""
    return path.read_text(encoding="utf-8")
```

Reads a PEM private key file as UTF-8 text.

```python
def write_private_key(path: Path, pem: str) -> None:
    """Write a private key readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(pem, encoding="utf-8")
```

Writes a private key readable by its owner only. It creates the folder if needed, creates the file with the permission `0o600` (octal 600: the owner may read and write, nobody else may do anything), sets the permission again with `chmod` in case the file already existed, and then writes the text. Permission is set **before** the secret is written, so there is no moment when it is readable by others.

```python
def sign(private_pem: str, message: bytes) -> str:
    """An ES256 signature (raw R||S, base64url) over `message`."""
    key = _ALGORITHM.prepare_key(private_pem)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    return b64url(_ALGORITHM.sign(message, key))
```

An ES256 signature over a message. `prepare_key` turns the PEM into a key object; `assert isinstance` narrows its type for the checker; `_ALGORITHM.sign` produces the signature in the raw `R||S` form (the two numbers of an ECDSA signature joined, 64 bytes), which is returned as base64url text.

```python
def verify(public_jwk: dict[str, Any], message: bytes, signature: str) -> bool:
    """True when `signature` is the key's ES256 signature over `message`."""
    try:
        key = ECAlgorithm.from_jwk(json.dumps(public_jwk))
        return bool(_ALGORITHM.verify(message, key, b64url_decode(signature)))
    except (ValueError, TypeError, jwt.PyJWTError):
        return False
```

True when the signature is the key's ES256 signature over the message. Any malformed key, malformed signature or library error becomes `False` (the `except` lists `ValueError`, `TypeError` and `jwt.PyJWTError`), so a bad input can never raise out of a check; `bool(...)` makes sure the result is a real true/false value.

```python
def thumbprint(jwk: dict[str, Any]) -> str:
    """The RFC 7638 thumbprint of an EC public key."""
    members = {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]}
    return b64url(hashlib.sha256(json.dumps(members, sort_keys=True, separators=(",", ":")).encode()).digest())
```

The **RFC 7638 thumbprint** of an elliptic-curve public key: a standard short fingerprint. Only the four members `crv`, `kty`, `x`, `y` are used, written as JSON with sorted keys and no spaces, hashed with SHA-256, and the digest is written in base64url. Two parties that hold the same key always compute the same thumbprint.

```python
def normalize_url(url: str) -> str:
    """The URL a proof is bound to: scheme, host and path; no query and no fragment."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
```

The URL a proof is bound to: scheme, host and path, with no query and no fragment. `urlsplit` splits the URL into parts and `urlunsplit` joins them again with the last two (query and fragment) empty.

```python
def access_token_hash(access_token: str) -> str:
    """The `ath` member of a proof: the base64url SHA-256 of the access token it accompanies."""
    return b64url(hashlib.sha256(access_token.encode()).digest())
```

The `ath` member of a proof: the base64url SHA-256 of the access token it accompanies. A proof that carries it cannot be reused with another token.

```python
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
```

Builds a **DPoP proof**: a small signed JWT (JSON Web Token) made for exactly one request. The claims are `jti` (a fresh random id, so a proof can be used once), `htm` (the HTTP method in capitals), `htu` (the normalized URL), `iat` (when it was issued, as whole seconds) and, when an access token goes with it, `ath` (the token's hash). `jwt.encode(..., algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": public_jwk})` signs it with the private key and puts the type and the public key in the header. The proof proves that the sender holds the private key right now, for this request.

### `src/open_banking/infrastructure/crypto.py` (123 lines)

```python
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
```

The cryptographic operations of the bank, behind the `Crypto` port. Imports: `hashlib` (scrypt and SHA-256), `hmac` (keyed hashes and constant-time comparison), `json`, `os` (`os.urandom` for random bytes), `secrets` (secure random numbers and tokens), `Callable`, the time types, PyJWT (`jwt`) and the `cryptography` library's `ec` module.

```python
class StandardCrypto:
    """Slow salted hashes for secrets, keyed digests for codes and tokens, and the checks on signed requests.

    `code_source` makes the one-time code replaceable in tests; production draws it from the system's secure source.
    """
```

The class docstring: slow salted hashes for secrets, keyed digests for codes and tokens, and the checks on signed requests. `code_source` makes the one-time code replaceable in tests; production draws it from the system's secure source.

```python
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
```

Keyword-only parameters (after `*`). `pepper` is a secret string known only to the bank process; it is encoded to bytes and used to key the digests. `scrypt_n` is the cost of password hashing (default `2**14`, which is 16384). `dpop_max_skew` is how far a proof's time may differ from the bank's clock (default 60 seconds). `code_source or (lambda: ...)` uses the given function, or by default a function that draws a number below a million with `secrets.randbelow(10**6)` and writes it as six digits with leading zeros (`:06d`).

```python
    def hash_secret(self, secret: str) -> str:
        """`scrypt$n$r$p$salt$hash`, with a fresh random salt."""
        salt = os.urandom(16)
        digest = hashlib.scrypt(secret.encode(), salt=salt, n=self._scrypt_n, r=8, p=1, dklen=32)
        return f"scrypt${self._scrypt_n}$8$1${keys.b64url(salt)}${keys.b64url(digest)}"
```

Hashes a PIN or a client secret with **scrypt**, a deliberately slow and memory-hungry hash, so guessing many secrets is expensive. A fresh 16-byte random **salt** (`os.urandom(16)`) makes two equal secrets hash differently. `hashlib.scrypt(secret.encode(), salt=salt, n=..., r=8, p=1, dklen=32)` returns a 32-byte hash (`n` is the cost, `r` the block size, `p` the parallelism). The result is one text, `scrypt$n$8$1$salt$hash`, with the settings stored beside the hash so they can change over time.

```python
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
```

Checks a secret against a stored hash. The stored text is split at `$` into its six parts (`stored.split("$")` raises `ValueError` if there are not exactly six, which is caught). A scheme other than `scrypt` is false. The secret is hashed again with the stored salt and cost settings, and `hmac.compare_digest` compares the two hashes in **constant time**, so how long the comparison takes does not reveal how many leading bytes matched. A malformed stored value gives `False`, never an exception.

```python
    def new_code(self) -> str:
        """A six-digit one-time code."""
        return self._code_source()
```

A six-digit one-time code from the code source.

```python
    def code_digest(self, challenge_id: str, code: str) -> str:
        """HMAC-SHA256 under the pepper, tied to the challenge, so a stolen digest cannot be tried offline."""
        return hmac.new(self._pepper, f"code:{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()
```

The digest stored instead of the code: **HMAC-SHA256** (a hash keyed with a secret) under the pepper, over the text `code:<challenge id>:<code>`. Tying it to the challenge means a digest cannot be reused for another challenge, and keying it means a stolen digest cannot be tried against all million codes offline without the pepper.

```python
    def new_token(self) -> str:
        """An unguessable bearer value."""
        return secrets.token_urlsafe(32)
```

An unguessable bearer value: `secrets.token_urlsafe(32)` draws 32 random bytes and writes them as URL-safe text.

```python
    def token_digest(self, token: str) -> str:
        """The keyed digest under which a bearer value is stored."""
        return hmac.new(self._pepper, f"token:{token}".encode(), hashlib.sha256).hexdigest()
```

The keyed digest under which a bearer value is stored, with the prefix `token:` so a token digest can never equal a code digest. The database never holds a usable token.

```python
    def thumbprint(self, jwk: Jwk) -> str:
        """The RFC 7638 thumbprint of a public key."""
        return keys.thumbprint(dict(jwk))
```

Delegates to `keys.thumbprint` (`dict(jwk)` copies the read-only mapping into a dictionary).

```python
    def verify_signature(self, public_jwk: Jwk, message: bytes, signature: str) -> bool:
        """True when `signature` is the key's signature over `message`."""
        return keys.verify(dict(public_jwk), message, signature)
```

Delegates to `keys.verify`.

```python
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
```

Checks a DPoP proof in stages. **Inside `try`:** read the header without verifying (`get_unverified_header`); the type must be `dpop+jwt` and the algorithm `ES256` (anything else is refused, which blocks algorithm-confusion tricks); if the proof embeds a public key it must have the same thumbprint as the registered one; build the registered key from its JWK; then `jwt.decode` verifies the signature, with the options switching off the standard time and audience checks (the bank does its own checks next). Any library error, bad value or missing field is turned into one `ClientAuthenticationError("invalid merchant credential")` (`from error` keeps the cause). **After `try`:** the method (`htm`) must equal the request's method and the URL (`htu`) the normalized request URL; the issue time (`iat`) must be within the allowed skew of `now` (`abs(now - issued_at) > self._max_skew`); when an access token is given, the proof's `ath` must equal the token's hash; when no token is given, the proof must not name one; and the unique id `jti` must be a non-empty text. The result is the id and the issue time.

```python
def _timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClientAuthenticationError("invalid merchant credential")
    return datetime.fromtimestamp(value, tz=UTC)
```

Converts the `iat` claim to a time. A `bool` is refused explicitly (`bool` is a kind of `int` in Python, so `True` would otherwise pass as 1), and anything that is not an integer is refused. `datetime.fromtimestamp(value, tz=UTC)` makes an aware time from seconds.

### `src/open_banking/infrastructure/otp.py` (67 lines)

```python
"""The one-time-code channel: where the bank 'sends' the code. The bank's own logs never contain it."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
```

The one-time-code channel: where the bank "sends" the code. The docstring: the bank's own logs never contain it. `threading` provides a lock; `Path` is a file path.

```python
class FileOtpChannel:
    """Appends each code as a JSON line to a file that stands in for the customer's phone.

    The file is created readable by its owner only. It is the only place a code is ever written in clear.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
```

Appends each code as a JSON line to a file that stands in for the customer's phone. The docstring: the file is created readable by its owner only, and it is the only place a code is ever written in clear.

```python
    def send(self, customer_id: str, code: str, purpose: str) -> None:
        """Append the message."""
        record = {
            "sent_at": datetime.now(UTC).isoformat(),
            "customer_id": customer_id,
            "code": code,
            "purpose": purpose,
        }
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch(mode=0o600, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as outbox:
                outbox.write(json.dumps(record) + "\n")
```

Builds a record (time, customer, code, purpose) and, under a lock so two threads do not interleave, creates the folder, creates the file with permission `0o600` if needed (`touch(mode=0o600, exist_ok=True)`) and appends one line of JSON (`open("a")` means append; `json.dumps(record) + "\n"`).

```python
    def latest_code(self, customer_id: str) -> str | None:
        """The newest code sent to `customer_id`, or None. Reading the phone, for the demo and the customer program."""
        if not self._path.exists():
            return None
        newest: str | None = None
        for line in self._path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["customer_id"] == customer_id:
                newest = str(record["code"])
        return newest
```

Reads the "phone": the newest code sent to one customer, or `None`. It reads all lines and keeps the code of the last matching record. Used by the demo and the customer program, which play the customer reading their device.

```python
    def latest_any(self) -> str | None:
        """The newest code in the file, whoever it was for: a demo phone has one owner."""
        if not self._path.exists():
            return None
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return str(json.loads(lines[-1])["code"]) if lines else None
```

The newest code in the file whoever it was for (a demo phone has one owner), or `None` for an empty or missing file.

```python
class MemoryOtpChannel:
    """Keeps the codes in memory, for tests."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []
```

Keeps the codes in memory, for tests: `messages` is a list of (customer, code, purpose) tuples.

```python
    def send(self, customer_id: str, code: str, purpose: str) -> None:
        """Remember the message."""
        self.messages.append((customer_id, code, purpose))
```

Remembers the message.

```python
    def latest_code(self, customer_id: str) -> str | None:
        """The newest code sent to `customer_id`, or None."""
        codes = [code for owner, code, _ in self.messages if owner == customer_id]
        return codes[-1] if codes else None
```

The newest code for a customer: a list comprehension collects the customer's codes (the `_` ignores the purpose) and the last one is returned, or `None`.

### `src/open_banking/infrastructure/faults.py` (61 lines)

```python
"""Scriptable network faults for the merchant's HTTP client: the bank processes a request, the answer never arrives."""

from __future__ import annotations

import threading
from collections import deque
from enum import StrEnum

import httpx
```

Scriptable network faults for the merchant's HTTP client: the bank processes a request, the answer never arrives. `deque` is a double-ended queue; `httpx` is the HTTP client library.

```python
class Fault(StrEnum):
    """What goes wrong with one request."""

    DROP_REQUEST = "DROP_REQUEST"  # the request never reaches the bank
    DROP_RESPONSE = "DROP_RESPONSE"  # the bank processes it and commits; the response is lost
```

What goes wrong with one request: `DROP_REQUEST` (it never reaches the bank) or `DROP_RESPONSE` (the bank processes it and commits; the response is lost).

```python
class FaultScript:
    """A queue of faults to apply to the next requests, in order. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._faults: deque[Fault] = deque()
```

A queue of faults to apply to the next requests, in order. The docstring says it is thread-safe: a lock guards the queue.

```python
    def push(self, fault: Fault, times: int = 1) -> None:
        """Queue `fault` for the next `times` requests."""
        with self._lock:
            self._faults.extend([fault] * times)
```

Queues the fault for the next `times` requests (`[fault] * times` makes a list that repeats it).

```python
    def next(self) -> Fault | None:
        """Take the next fault, if any."""
        with self._lock:
            return self._faults.popleft() if self._faults else None
```

Takes the next fault from the front (`popleft`), or `None` when the queue is empty.

```python
class FaultInjectingTransport(httpx.BaseTransport):
    """Wraps a real transport. With a scripted fault it either drops the request before it is sent, or sends it,
    lets the server finish, and then raises a read timeout instead of returning the response: exactly what a dropped
    connection on the way back looks like to the caller.
    """

    def __init__(self, inner: httpx.BaseTransport, script: FaultScript) -> None:
        self._inner = inner
        self._script = script
```

Wraps a real transport. The docstring explains: with a scripted fault it either drops the request before it is sent, or sends it, lets the server finish, and then raises a read timeout instead of returning the response, exactly what a dropped connection on the way back looks like to the caller.

```python
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Send the request, or fail it the way the script says."""
        fault = self._script.next()
        if fault is Fault.DROP_REQUEST:
            raise httpx.ConnectTimeout("scripted: the request never reached the bank", request=request)
        response = self._inner.handle_request(request)
        if fault is Fault.DROP_RESPONSE:
            response.read()
            response.close()
            raise httpx.ReadTimeout("scripted: the bank processed the request but its answer was lost", request=request)
        return response
```

Sends the request or fails it as scripted. `DROP_REQUEST` raises `httpx.ConnectTimeout` before anything is sent. Otherwise the request goes to the real transport; for `DROP_RESPONSE` the response is read completely and closed (so the server finishes and commits) and then `httpx.ReadTimeout` is raised. This reproduces the dangerous case: the payment was made, the merchant never learns it.

```python
    def close(self) -> None:
        """Close the wrapped transport."""
        self._inner.close()
```

Closes the wrapped transport.

### `src/open_banking/infrastructure/settings.py` (107 lines)

```python
"""Configuration from the environment, validated once at startup (twelve-factor). Secrets only from here."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
```

Configuration from the environment, validated once at startup (twelve-factor: settings come from environment variables and secrets come only from there). `os` reads the environment; `Mapping` is a read-only dictionary type; `Path` is a file path.

```python
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
```

The accepted log levels, as a set.

```python
class ConfigurationError(ValueError):
    """An environment variable is missing or has an unusable value."""
```

A named error for a missing or unusable setting.

```python
def read_text(environ: Mapping[str, str], name: str, default: str | None = None) -> str:
    """A required, non-empty setting, or the default."""
    value = environ.get(name, default if default is not None else "").strip()
    if not value:
        raise ConfigurationError(f"{name} must be set and not empty")
    return value
```

A required, non-empty text setting, or its default. `environ.get(name, default if default is not None else "")` takes the variable or the default (or an empty string), `.strip()` removes spaces at both ends and an empty result raises `ConfigurationError`. A setting with no default, such as a password, therefore fails loudly when it is missing.

```python
def read_positive_int(environ: Mapping[str, str], name: str, default: int, *, maximum: int | None = None) -> int:
    """A positive integer setting, at most `maximum` when given."""
    raw = environ.get(name, str(default)).strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ConfigurationError(f"{name} must be a positive integer, got {raw!r}")
    if maximum is not None and int(raw) > maximum:
        raise ConfigurationError(f"{name} must be at most {maximum}, got {raw}")
    return int(raw)
```

A whole-number setting. `raw.isdigit()` is true for text made only of the digits 0-9; a value that is not digits or is below 1 is refused; if `maximum` is given a larger value is refused too (the `*` makes `maximum` keyword-only).

```python
def read_url(environ: Mapping[str, str], name: str, default: str) -> str:
    """An http(s) URL setting without a trailing slash."""
    value = read_text(environ, name, default).rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ConfigurationError(f"{name} must start with http:// or https://, got {value!r}")
    return value
```

An http or https URL setting without a trailing slash (`rstrip("/")`). `value.startswith(("http://", "https://"))` tests several beginnings at once.

```python
def read_level(environ: Mapping[str, str]) -> str:
    """LOG_LEVEL, upper-cased and checked."""
    level = read_text(environ, "LOG_LEVEL", "INFO").upper()
    if level not in _LEVELS:
        raise ConfigurationError(f"LOG_LEVEL must be one of {sorted(_LEVELS)}, got {level!r}")
    return level
```

`LOG_LEVEL`, upper-cased and checked against the allowed set (`sorted(_LEVELS)` lists them in the message).

```python
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
```

The PostgreSQL part of the configuration. `password: str = field(repr=False)` leaves the password out of the printed form of the object, so printing or logging the settings can never reveal it.

```python
@dataclass(frozen=True, slots=True)
class ServerSettings:
    """Where an HTTP API listens and how long it waits for in-flight requests when told to stop."""

    host: str
    port: int
    shutdown_grace_seconds: int
```

Where an HTTP API listens (host, port) and how many seconds it waits for in-flight requests when told to stop.

```python
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
```

Everything the merchant process reads from the environment. The docstring: it has no database and no bank secrets. It holds the server settings, the bank's URL, the merchant's client id and secret (the secret is left out of the printed form), the file with its private key, the account it is paid into, the request timeout, the number of attempts, the first retry delay in milliseconds and the log level.

```python
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
```

A class method (`@classmethod` above; `cls` is the class) that builds the configuration. The environment defaults to `os.environ`; tests pass a plain dictionary. Defaults: `MERCHANT_HOST` `127.0.0.1`, `MERCHANT_PORT` 8464, `SHUTDOWN_GRACE_SECONDS` 10, `BANK_URL` `http://127.0.0.1:8463`, `MERCHANT_CLIENT_ID` `merchant-1`, `MERCHANT_CLIENT_SECRET` (no default), `MERCHANT_PRIVATE_KEY_FILE` `var/merchant-1-key.pem`, `MERCHANT_PAYEE_ACCOUNT` `acct-shop`, `BANK_TIMEOUT_SECONDS` 5, `BANK_RETRY_ATTEMPTS` 3 (at most 10) and `BANK_RETRY_BASE_DELAY_MS` 50. The docstring: it raises `ConfigurationError` naming the first bad variable.

### `src/open_banking/infrastructure/bank_settings.py` (79 lines)

```python
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
```

The bank process's configuration, read from the environment and validated once at startup. It reuses the small readers from `settings.py`.

```python
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
```

Everything the bank process reads from the environment: the database settings, the server settings, its public URL, the timing rules (`BankSettings`), the **pepper** (a secret that keys the digests of one-time codes and bearer values; left out of the printed form), the file with the bank's signing key, the file standing in for the customer's phone, the scrypt cost and the log level.

```python
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
```

Builds the configuration. The pepper is required and must be at least 16 characters. Defaults: `BANK_PORT` 8463, `BANK_HOST` `127.0.0.1`; database `POSTGRES_HOST` `localhost`, `POSTGRES_PORT` 54613, `POSTGRES_DB` and `POSTGRES_USER` `openbanking`, `POSTGRES_PASSWORD` (no default), `POSTGRES_CONNECT_TIMEOUT_SECONDS` 5, `DB_POOL_MAX_SIZE` 20; `BANK_PUBLIC_URL` built from the host and port; timings in minutes or days: challenge 5 minutes, proof 2 minutes, token 10 minutes, single-payment consent 10 minutes, recurring consent 90 days, DPoP clock skew 60 seconds and idempotency keys 24 hours; `BANK_SIGNING_KEY_FILE` `var/bank-signing-key.pem`, `OTP_OUTBOX_FILE` `var/otp-outbox.log`. The scrypt cost comes from `_scrypt_n`.

```python
def _scrypt_n(env: Mapping[str, str]) -> int:
    n = read_positive_int(env, "SCRYPT_N", 2**14)
    if n < 2 or n & (n - 1):
        raise ConfigurationError(f"SCRYPT_N must be a power of two, got {n}")
    return n
```

Reads `SCRYPT_N` (default `2**14`). scrypt requires a power of two: `n & (n - 1)` is zero only for powers of two (it clears the lowest set bit, leaving zero when there was only one), so any other value, or a value below 2, is refused.

### `src/open_banking/infrastructure/bank_client.py` (152 lines)

```python
"""The merchant's client for the bank: a key-bound token, a proof for every request, a signed request, the same
idempotency key on every retry, and a check of the bank's signature on what comes back."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from open_banking.application.logs import fields
from open_banking.application.merchant.checkout import BankUnavailableError, Paid, Refused
from open_banking.domain.errors import ClientAuthenticationError
from open_banking.domain.money import Money
from open_banking.domain.signing import canonical_payment_request
from open_banking.infrastructure import keys
```

The merchant's client for the bank. The module docstring (two lines) lists what it does: a key-bound token, a proof for every request, a signed request, the same idempotency key on every retry, and a check of the bank's signature on what comes back. `time` provides `sleep`; `httpx` is the HTTP client; `keys` has the signing helpers.

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
_RETRYABLE_STATUSES = {502, 503, 504}
```

The HTTP statuses that mean "the server or a gateway failed, try again": 502 Bad Gateway, 503 Service Unavailable and 504 Gateway Timeout.

```python
class HttpBankGateway:
    """`BankGateway` over HTTP. Timeouts and 5xx answers are retried with exponential backoff and the same key; the
    bank either does the work now or replays the answer it stored for that key, so a retry never pays twice.
    """
```

`BankGateway` over HTTP. The docstring: timeouts and 5xx answers are retried with exponential backoff and the same key; the bank either does the work now or replays the answer it stored for that key, so a retry never pays twice.

```python
    def __init__(
        self,
        client: httpx.Client,
        *,
        bank_url: str,
        client_id: str,
        client_secret: str,
        private_pem: str,
        attempts: int,
        retry_base_delay_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        bank_public_jwk: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._bank_url = bank_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._private_pem = private_pem
        self._public_jwk = keys.public_jwk_of(private_pem)
        self._attempts = attempts
        self._base_delay = retry_base_delay_seconds
        self._clock = clock
        self._sleep = sleep
        self._bank_public_jwk = bank_public_jwk
        self._token: tuple[str, datetime] | None = None
```

Keyword-only arguments: the HTTP client, the bank's URL (trailing slash removed), the merchant's id, secret and private key (PEM), the number of attempts and the first delay. `clock` and `sleep` are injectable so tests need not wait (the defaults are a lambda that reads the real time and `time.sleep`). `bank_public_jwk` is optional: when known, results must be signed by the bank. The merchant's public key is derived from its private key. `_token` caches the current access token and its expiry.

```python
    def initiate_payment(
        self, *, consent_id: str, payee_account: str, amount: Money, idempotency_key: str
    ) -> Paid | Refused:
        """Raises `BankUnavailableError` when every attempt timed out or failed with a 5xx."""
        body = {
            "consent_id": consent_id,
            "payee_account": payee_account,
            "amount_minor": amount.minor,
            "currency": amount.currency,
        }
        signature = keys.sign(
            self._private_pem,
            canonical_payment_request(
                client_id=self._client_id,
                consent_id=consent_id,
                payee_account=payee_account,
                amount=amount,
                idempotency_key=idempotency_key,
            ),
        )
        last_error = "no attempt was made"
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._post_payment(body, idempotency_key, signature)
            except httpx.TransportError as error:
                last_error = type(error).__name__
                logger.warning(
                    "bank did not answer; retrying with the same key",
                    extra=fields(attempt=attempt, error=last_error, idempotency_key=idempotency_key),
                )
            else:
                if response.status_code == 401:
                    self._token = None  # the token may have expired; one fresh token per attempt
                    last_error = "401"
                elif response.status_code in _RETRYABLE_STATUSES:
                    last_error = str(response.status_code)
                else:
                    return self._result(response)
            if attempt < self._attempts:
                self._sleep(self._base_delay * 2 ** (attempt - 1))
        raise BankUnavailableError(f"the bank did not answer after {self._attempts} attempts ({last_error})")
```

The retry loop. `body` is the JSON body. The request is **signed once** before the loop with `keys.sign` over the canonical bytes (the idempotency key is in them), so every retry carries the same signature and the same key. Then for each attempt from 1 to the maximum: try to POST; a `httpx.TransportError` (timeout, connection error) is logged as a warning and noted; an answer with status 401 forgets the cached token (it may have expired, so the next attempt fetches a fresh one) and counts as a failed attempt; a 5xx retryable status counts as failed; any other answer is turned into a result and returned. Between attempts it sleeps `base delay * 2 ** (attempt - 1)` seconds (exponential backoff: 1x, 2x, 4x ...). After the last attempt it raises `BankUnavailableError` saying how many attempts were made and the last problem.

```python
    def _post_payment(self, body: dict[str, Any], idempotency_key: str, signature: str) -> httpx.Response:
        url = f"{self._bank_url}/v1/payments"
        token = self._access_token()
        proof = keys.build_dpop_proof(
            self._private_pem, self._public_jwk, method="POST", url=url, now=self._clock(), access_token=token
        )
        return self._client.post(
            url,
            json=body,
            headers={
                "Authorization": f"DPoP {token}",
                "DPoP": proof,
                "Idempotency-Key": idempotency_key,
                "X-Request-Signature": signature,
            },
        )
```

One POST to `/v1/payments`. It gets an access token (cached), builds a **fresh DPoP proof for this request** bound to the method, the URL and the token, and sends the JSON body with four headers: `Authorization: DPoP <token>`, `DPoP: <proof>`, `Idempotency-Key` and `X-Request-Signature`.

```python
    def _access_token(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._token[1]:
            return self._token[0]
        url = f"{self._bank_url}/oauth/token"
        proof = keys.build_dpop_proof(self._private_pem, self._public_jwk, method="POST", url=url, now=now)
        response = self._client.post(
            url,
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
            headers={"DPoP": proof},
        )
        if response.status_code != 200:
            raise ClientAuthenticationError("the bank refused the merchant's credential")
        payload = response.json()
        lifetime = max(int(payload["expires_in"]) - 30, 1)  # refresh a little early
        self._token = (payload["access_token"], now + timedelta(seconds=lifetime))
        return str(payload["access_token"])
```

Returns a cached token while it is still valid. Otherwise it builds a DPoP proof for `POST /oauth/token` (no token yet), posts `grant_type=client_credentials` as a form (`data=`) with the merchant's id and secret as HTTP Basic credentials (`auth=(id, secret)`), and treats any status other than 200 as a refusal of the credential. The token's lifetime is `expires_in` minus 30 seconds (refresh a little early; `max(..., 1)` keeps it at least one second), and the token and its expiry are cached.

```python
    def _result(self, response: httpx.Response) -> Paid | Refused:
        payload = response.json()
        if response.status_code == 201:
            self._check_bank_signature(response)
            return Paid(payload["payment_id"])
        error = payload.get("error", {})
        return Refused(str(error.get("code", "refused")), str(error.get("message", "the bank refused the payment")))
```

Turns the bank's answer into a result. Status 201 means the payment was posted: the bank's signature on the body is checked, then `Paid` with the payment id is returned. Anything else is a refusal: `Refused` with the error code and message from the body (`payload.get("error", {})` and `.get(..., default)` supply fallbacks).

```python
    def _check_bank_signature(self, response: httpx.Response) -> None:
        """When the bank's public key is known, the result must carry the bank's signature over its exact body."""
        if self._bank_public_jwk is None:
            return
        signature = response.headers.get("Bank-Signature", "")
        message = json.dumps(response.json(), sort_keys=True, separators=(",", ":")).encode()
        if not keys.verify(self._bank_public_jwk, message, signature):
            raise ClientAuthenticationError("the payment result is not signed by the bank")
```

When the bank's public key is known, the result must carry the bank's signature over its exact body: the response body is written canonically (sorted keys, no spaces) and verified against the `Bank-Signature` header. A missing or wrong signature raises `ClientAuthenticationError`, so a payment confirmation cannot be forged on the way back. Without a configured key the check is skipped.

### `src/open_banking/infrastructure/postgres.py` (498 lines)

```python
"""PostgreSQL: the connection pool, the unit of work, and the repositories behind the ports."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from open_banking.application.bank.ports import (
    AccessToken,
    Account,
    Claim,
    Customer,
    Fresh,
    OAuthClient,
    PaymentRecord,
    Replay,
    Reused,
    UnitOfWork,
)
from open_banking.domain.authentication import Challenge, ChallengeStatus
from open_banking.domain.consent import AuthenticationProof, Consent, ConsentScope, ConsentTerms
from open_banking.domain.idempotency import IdempotencyKey, StoredResponse
from open_banking.domain.ledger import Transfer
from open_banking.domain.money import Money
from open_banking.infrastructure.settings import PostgresSettings
```

PostgreSQL: the connection pool, the unit of work and the repositories behind the ports. `contextmanager` turns a generator function into something usable in a `with` block; `psycopg` is the database driver; `make_conninfo` builds the connection text; `Jsonb` marks a Python dictionary to be stored as PostgreSQL JSON; `ConnectionPool` keeps open connections ready. The long import from `application.bank.ports` names the types the repositories implement.

```python
def conninfo(settings: PostgresSettings) -> str:
    """The libpq connection string for `settings`; sessions run in UTC."""
    return make_conninfo(
        host=settings.host,
        port=settings.port,
        dbname=settings.database,
        user=settings.user,
        password=settings.password,
        connect_timeout=settings.connect_timeout_seconds,
        application_name="open-banking",
        options="-c timezone=UTC",
    )
```

Returns the connection string for `libpq` (PostgreSQL's client library): host, port, database name, user, password, a connect timeout, `application_name="open-banking"` (so connections are recognizable in the server's list of sessions) and `options="-c timezone=UTC"` (sessions run in UTC).

```python
def open_pool(settings: PostgresSettings) -> ConnectionPool[psycopg.Connection[Any]]:
    """Open a pool and wait until it holds a working connection, or fail within the connect timeout."""
    pool: ConnectionPool[psycopg.Connection[Any]] = ConnectionPool(
        conninfo(settings), min_size=1, max_size=settings.pool_max_size, open=False, timeout=15.0
    )
    pool.open(wait=True, timeout=float(settings.connect_timeout_seconds) * 6)
    return pool
```

Creates a pool without opening it (`open=False`) with at least one and at most `pool_max_size` connections and a 15-second wait for a free connection, then `pool.open(wait=True, timeout=...)` starts it and **waits** until a working connection exists or the timeout passes. A missing database becomes a loud error at startup.

```python
class PostgresAccounts:
    """`AccountRepository` over `bank_accounts`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection
```

The repository for `bank_accounts`. It receives one open connection.

```python
    def get(self, account_id: str) -> Account | None:
        """Read an account."""
        return self._read(
            "SELECT account_id, owner, currency, vault, balance_minor FROM bank_accounts WHERE account_id = %s",
            account_id,
        )
```

Reads an account. `%s` marks a value to be filled in; the values are passed separately in a tuple, so a value can never be read as SQL.

```python
    def get_for_update(self, account_id: str) -> Account | None:
        """Read and lock the account until the transaction ends."""
        return self._read(
            "SELECT account_id, owner, currency, vault, balance_minor FROM bank_accounts "
            "WHERE account_id = %s FOR UPDATE",
            account_id,
        )
```

The same query with `FOR UPDATE` added, which locks the row until the transaction ends so a payment cannot race another on the same balance.

```python
    def _read(self, sql: str, account_id: str) -> Account | None:
        row = self._connection.execute(sql, (account_id,)).fetchone()
        if row is None:
            return None
        account, owner, currency, vault, balance = row
        return Account(account, owner, Money(balance, currency.strip()), vault)
```

Runs the query; `fetchone()` returns a row or `None`. The row is unpacked into named variables and an `Account` is built (the currency is `strip()`ped because the column is `CHAR(3)` and may be padded).

```python
class PostgresCustomers:
    """`CustomerRepository` over `customers`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection
```

The repository for `customers`.

```python
    def by_username(self, username: str) -> Customer | None:
        """Find by username."""
        return self._read("username = %s", username)
```

Find by username, through the shared `_read`.

```python
    def by_id(self, customer_id: str) -> Customer | None:
        """Find by id."""
        return self._read("customer_id = %s", customer_id)
```

Find by id.

```python
    def add(self, customer: Customer) -> None:
        """Enroll a customer."""
        self._connection.execute(
            "INSERT INTO customers (customer_id, username, account_id, pin_hash) VALUES (%s, %s, %s, %s)",
            (customer.customer_id, customer.username, customer.account_id, customer.pin_hash),
        )
```

Enroll a customer: one `INSERT`.

```python
    def _read(self, condition: str, value: str) -> Customer | None:
        row = self._connection.execute(
            f"SELECT customer_id, username, account_id, pin_hash FROM customers WHERE {condition}", (value,)
        ).fetchone()
        return None if row is None else Customer(str(row[0]), row[1], row[2], row[3])
```

The condition text comes only from the two fixed callers above (never from user input), and the value goes through `%s`. `None if row is None else Customer(...)` is a one-line `if`/`else`; `str(row[0])` turns the UUID column into text.

```python
class PostgresClients:
    """`ClientRepository` over `oauth_clients`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def get(self, client_id: str) -> OAuthClient | None:
        """Find a client."""
        row = self._connection.execute(
            "SELECT client_id, name, secret_hash, public_jwk FROM oauth_clients WHERE client_id = %s", (client_id,)
        ).fetchone()
        return None if row is None else OAuthClient(row[0], row[1], row[2], row[3])

    def add(self, client: OAuthClient) -> None:
        """Register a client."""
        self._connection.execute(
            "INSERT INTO oauth_clients (client_id, name, secret_hash, public_jwk) VALUES (%s, %s, %s, %s)",
            (client.client_id, client.name, client.secret_hash, Jsonb(dict(client.public_jwk))),
        )
```

The repository for `oauth_clients` (the registered merchants). `get` selects by id; `add` inserts, storing the public key as JSON with `Jsonb(dict(...))`.

```python
class PostgresChallenges:
    """`ChallengeRepository` over `authentication_challenges`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def add(self, challenge: Challenge) -> None:
        """Store a new challenge."""
        self._connection.execute(
            "INSERT INTO authentication_challenges (challenge_id, customer_id, terms_digest, code_digest, created_at, "
            "expires_at, attempts, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                challenge.challenge_id,
                challenge.customer_id,
                challenge.terms_digest,
                challenge.code_digest,
                challenge.created_at,
                challenge.expires_at,
                challenge.attempts,
                challenge.status.value,
            ),
        )
```

The repository for `authentication_challenges`. `add` inserts every field.

```python
    def get_for_update(self, challenge_id: str) -> Challenge | None:
        """Read and lock a challenge."""
        try:
            row = self._connection.execute(
                "SELECT challenge_id, customer_id, terms_digest, code_digest, created_at, expires_at, attempts, status "
                "FROM authentication_challenges WHERE challenge_id = %s FOR UPDATE",
                (challenge_id,),
            ).fetchone()
        except psycopg.errors.InvalidTextRepresentation:
            self._connection.rollback()
            return None
        if row is None:
            return None
        cid, customer, terms, code, created, expires, attempts, status = row
        return Challenge(str(cid), str(customer), terms, code, created, expires, attempts, ChallengeStatus(status))
```

Reads and locks a challenge. A text that is not a valid UUID makes PostgreSQL raise `InvalidTextRepresentation`; that is caught, the failed transaction is rolled back and `None` is returned, so a made-up id behaves like an unknown one.

```python
    def save(self, challenge: Challenge) -> None:
        """Write the attempts and status."""
        self._connection.execute(
            "UPDATE authentication_challenges SET attempts = %s, status = %s WHERE challenge_id = %s",
            (challenge.attempts, challenge.status.value, challenge.challenge_id),
        )
```

Writes the attempts and status.

```python
class PostgresProofs:
    """`ProofRepository` over `authentication_proofs`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def add(self, proof: AuthenticationProof) -> None:
        """Store a proof."""
        self._connection.execute(
            "INSERT INTO authentication_proofs (proof_id, customer_id, terms_digest, expires_at, consumed) "
            "VALUES (%s, %s, %s, %s, %s)",
            (proof.proof_id, proof.customer_id, proof.terms_digest, proof.expires_at, proof.consumed),
        )

    def get_for_update(self, proof_id: str) -> AuthenticationProof | None:
        """Read and lock a proof."""
        row = self._connection.execute(
            "SELECT proof_id, customer_id, terms_digest, expires_at, consumed FROM authentication_proofs "
            "WHERE proof_id = %s FOR UPDATE",
            (proof_id,),
        ).fetchone()
        return None if row is None else AuthenticationProof(row[0], str(row[1]), row[2], row[3], row[4])

    def mark_consumed(self, proof_id: str) -> None:
        """A proof is good for one consent."""
        self._connection.execute("UPDATE authentication_proofs SET consumed = TRUE WHERE proof_id = %s", (proof_id,))
```

The repository for `authentication_proofs`: `add`, `get_for_update` and `mark_consumed` (`UPDATE ... SET consumed = TRUE`).

```python
class PostgresConsents:
    """`ConsentRepository` over `consents`."""

    _COLUMNS = (
        "consent_id, customer_id, payer_account, payee_account, client_id, currency, max_amount_minor, scope, "
        "period_seconds, created_at, expires_at, revoked_at, used_at"
    )

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection
```

The repository for `consents`. `_COLUMNS` is the column list written once so the `INSERT` and `SELECT` stay in step; it contains no user input.

```python
    def add(self, consent: Consent) -> None:
        """Store a consent."""
        terms = consent.terms
        self._connection.execute(
            f"INSERT INTO consents ({self._COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                consent.consent_id,
                consent.customer_id,
                terms.payer_account,
                terms.payee_account,
                terms.client_id,
                terms.max_amount.currency,
                terms.max_amount.minor,
                terms.scope.value,
                None if terms.period is None else int(terms.period.total_seconds()),
                consent.created_at,
                consent.expires_at,
                consent.revoked_at,
                consent.used_at,
            ),
        )
```

Stores a consent: the placeholders are generated for the thirteen columns, and the period is stored as a whole number of seconds or `NULL`.

```python
    def get_for_update(self, consent_id: str) -> Consent | None:
        """Read and lock a consent, so a revocation and a payment under it run one after the other."""
        try:
            row = self._connection.execute(
                f"SELECT {self._COLUMNS} FROM consents WHERE consent_id = %s FOR UPDATE", (consent_id,)
            ).fetchone()
        except psycopg.errors.InvalidTextRepresentation:
            self._connection.rollback()
            return None
        if row is None:
            return None
        (cid, customer, payer, payee, client, currency, maximum, scope, period, created, expires, revoked, used) = row
        terms = ConsentTerms(
            payer,
            payee,
            client,
            Money(maximum, currency.strip()),
            ConsentScope(scope),
            None if period is None else timedelta(seconds=period),
        )
        return Consent(str(cid), str(customer), terms, created, expires, revoked, used)
```

Reads and locks a consent (the docstring: so a revocation and a payment under it run one after the other). The same `InvalidTextRepresentation` handling as for challenges. The row is unpacked, the terms are rebuilt as a `ConsentTerms` (the scope text becomes a `ConsentScope`, the seconds become a `timedelta`) and the `Consent` is returned.

```python
    def save(self, consent: Consent) -> None:
        """Write the revocation and use times."""
        self._connection.execute(
            "UPDATE consents SET revoked_at = %s, used_at = %s WHERE consent_id = %s",
            (consent.revoked_at, consent.used_at, consent.consent_id),
        )
```

Writes the revocation and use times.

```python
    def count(self) -> int:
        """How many consents exist."""
        row = self._connection.execute("SELECT count(*) FROM consents").fetchone()
        assert row is not None
        return int(row[0])
```

`SELECT count(*)`; `assert row is not None` tells the type checker the query always returns a row.

```python
    def spent_since(self, consent_id: str, since: datetime, currency: str) -> Money:
        """What payments under this consent moved at or after `since`."""
        row = self._connection.execute(
            "SELECT COALESCE(sum(amount_minor), 0)::bigint FROM payments "
            "WHERE consent_id = %s AND created_at >= %s AND currency = %s",
            (consent_id, since, currency),
        ).fetchone()
        assert row is not None
        return Money(row[0], currency)
```

What payments under the consent moved at or after a moment: `COALESCE(sum(amount_minor), 0)::bigint` sums them (0 when there are none; `::bigint` casts the sum, which PostgreSQL types as `NUMERIC`, back to an integer).

```python
class PostgresTokens:
    """`TokenRepository` over `access_tokens` and `seen_proofs`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def add(self, token: AccessToken) -> None:
        """Store an issued token."""
        self._connection.execute(
            "INSERT INTO access_tokens (token_digest, client_id, key_thumbprint, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (token.token_digest, token.client_id, token.key_thumbprint, token.created_at, token.expires_at),
        )
```

The repository for `access_tokens` and `seen_proofs`. `add` stores a token (its digest, never the token).

```python
    def get(self, token_digest: str) -> AccessToken | None:
        """Find a token by the digest of its value."""
        row = self._connection.execute(
            "SELECT token_digest, client_id, key_thumbprint, created_at, expires_at FROM access_tokens "
            "WHERE token_digest = %s",
            (token_digest,),
        ).fetchone()
        return None if row is None else AccessToken(row[0], row[1], row[2], row[3], row[4])
```

Finds a token by the digest of its value.

```python
    def remember_proof(self, jti: str, at: datetime) -> bool:
        """Record a proof id; False when it was seen before (a replay)."""
        row = self._connection.execute(
            "INSERT INTO seen_proofs (jti, seen_at) VALUES (%s, %s) ON CONFLICT (jti) DO NOTHING RETURNING jti",
            (jti, at),
        ).fetchone()
        return row is not None
```

Records a proof id. `INSERT ... ON CONFLICT (jti) DO NOTHING RETURNING jti` inserts the id if it is new and returns it; if it already exists nothing is inserted and no row comes back. So `row is not None` is true exactly for a first use: the database itself detects a replay, even between two requests at the same instant.

```python
class PostgresPayments:
    """`PaymentRepository` over `payments`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def add(self, payment: PaymentRecord) -> None:
        """Store a payment."""
        self._connection.execute(
            "INSERT INTO payments (payment_id, consent_id, client_id, payer_account, payee_account, amount_minor, "
            "currency, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                payment.payment_id,
                payment.consent_id,
                payment.client_id,
                payment.payer_account,
                payment.payee_account,
                payment.amount.minor,
                payment.amount.currency,
                payment.created_at,
            ),
        )
```

The repository for `payments`: `add` inserts a payment.

```python
    def get(self, payment_id: str, client_id: str) -> PaymentRecord | None:
        """Find a payment made for this client."""
        try:
            row = self._connection.execute(
                "SELECT payment_id, consent_id, client_id, payer_account, payee_account, amount_minor, currency, "
                "created_at FROM payments WHERE payment_id = %s AND client_id = %s",
                (payment_id, client_id),
            ).fetchone()
        except psycopg.errors.InvalidTextRepresentation:
            self._connection.rollback()
            return None
        if row is None:
            return None
        pid, consent, client, payer, payee, amount, currency, created = row
        return PaymentRecord(str(pid), str(consent), client, payer, payee, Money(amount, currency.strip()), created)
```

Finds a payment by id **and** merchant, so one merchant cannot read another's payment. The same `InvalidTextRepresentation` handling as above.

```python
class PostgresLedger:
    """`LedgerRepository` over `journal_entries`, `postings` and the balances of `bank_accounts`."""

    def __init__(self, connection: psycopg.Connection[Any], new_id: Any) -> None:
        self._connection = connection
        self._new_id = new_id
```

The repository for `journal_entries`, `postings` and the balances in `bank_accounts`. It receives a function that makes ids.

```python
    def post(self, transfer: Transfer, kind: str, reference: str, at: datetime) -> None:
        """Append the transfer's two postings and move the two balances; the deferred triggers check them together."""
        entry_id = self._new_id()
        currency = transfer.amount.currency
        with self._connection.pipeline():
            self._connection.execute(
                "INSERT INTO journal_entries (id, kind, reference, created_at) VALUES (%s, %s, %s, %s)",
                (entry_id, kind, reference, at),
            )
            for posting in transfer.postings():
                self._connection.execute(
                    "INSERT INTO postings (entry_id, account_id, amount_minor, currency, created_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (entry_id, posting.account_id, posting.amount.minor, currency, at),
                )
                self._connection.execute(
                    "UPDATE bank_accounts SET balance_minor = balance_minor + %s WHERE account_id = %s",
                    (posting.amount.minor, posting.account_id),
                )
```

Appends a transfer. Inside `with self._connection.pipeline():` (statements are sent in one batch without waiting for each answer) it inserts one journal entry, then for each of the two postings inserts the posting row and adds the signed amount to the account's balance (`balance_minor = balance_minor + %s`). The docstring: the deferred triggers check the postings and the balances together at commit, so the database itself refuses a transfer that leaves a balance different from the sum of its postings.

```python
    def total(self, currency: str) -> Money:
        """The sum of every posting in `currency`."""
        row = self._connection.execute(
            "SELECT COALESCE(sum(amount_minor), 0)::bigint FROM postings WHERE currency = %s", (currency,)
        ).fetchone()
        assert row is not None
        return Money(row[0], currency)
```

The sum of every posting in a currency (zero when the ledger is whole).

```python
class PostgresIdempotencyKeys:
    """`IdempotencyStore` over `idempotency_keys`, scoped to the merchant.

    `claim` takes a transaction-scoped advisory lock on (merchant, key), so concurrent callers of one key queue up and
    each sees the previous caller's committed row. The primary key is the backstop if that ever fails.
    """

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection
```

The key store, scoped to the merchant. The docstring explains `claim`: it takes a transaction-scoped **advisory lock** on (merchant, key), so concurrent callers of one key queue up and each sees the previous caller's committed row; the primary key is the backstop if that ever fails.

```python
    def claim(self, client_id: str, key: IdempotencyKey, request_fingerprint: str, now: datetime) -> Claim:
        """Lock the key and say whether to do the work, replay the stored response, or refuse a reused key."""
        self._connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{client_id}\x1f{key.value}",)
        )
        row = self._connection.execute(
            "SELECT request_fingerprint, response_status, response_body, expires_at FROM idempotency_keys "
            "WHERE client_id = %s AND key = %s",
            (client_id, key.value),
        ).fetchone()
        if row is None:
            return Fresh()
        stored_fingerprint, status, body, expires_at = row
        if expires_at <= now:
            self._connection.execute(
                "DELETE FROM idempotency_keys WHERE client_id = %s AND key = %s", (client_id, key.value)
            )
            return Fresh()
        if stored_fingerprint != request_fingerprint:
            return Reused()
        return Replay(StoredResponse(status, body))
```

First `SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))` locks a 64-bit number derived from the text `<client>\x1f<key>` (`\x1f` is an unprintable separator character, so two different pairs cannot make the same text); the lock is held until the transaction ends. Then the stored row for the pair is read. No row: `Fresh()`. An expired row (`expires_at <= now`) is deleted and treated as `Fresh()`. A row whose fingerprint differs: `Reused()`. Otherwise `Replay(StoredResponse(status, body))` with the stored answer.

```python
    def store(
        self,
        client_id: str,
        key: IdempotencyKey,
        request_fingerprint: str,
        response: StoredResponse,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        """Record the response under the key, in the transaction that made the effect."""
        self._connection.execute(
            "INSERT INTO idempotency_keys (client_id, key, request_fingerprint, response_status, response_body, "
            "created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (client_id, key.value, request_fingerprint, response.status, Jsonb(dict(response.body)), now, expires_at),
        )
```

Inserts the key, the fingerprint, the response status, the response body wrapped in `Jsonb(...)` and the two times, in the transaction that made the effect.

```python
@dataclass(slots=True)
class PostgresUnitOfWork:
    """The repositories of one connection, all inside the transaction the pool opened for it."""

    accounts: PostgresAccounts
    customers: PostgresCustomers
    clients: PostgresClients
    challenges: PostgresChallenges
    proofs: PostgresProofs
    consents: PostgresConsents
    tokens: PostgresTokens
    payments: PostgresPayments
    ledger: PostgresLedger
    keys: PostgresIdempotencyKeys
```

A plain bundle of the ten repositories for one connection.

```python
class PostgresUnitOfWorkFactory:
    """Open a transaction on a pooled connection: commit on a normal exit, roll back on an exception."""

    def __init__(self, pool: ConnectionPool[psycopg.Connection[Any]], new_id: Any) -> None:
        self._pool = pool
        self._new_id = new_id

    @contextmanager
    def __call__(self) -> Iterator[UnitOfWork]:
        """One unit of work."""
        with self._pool.connection() as connection:
            yield PostgresUnitOfWork(
                PostgresAccounts(connection),
                PostgresCustomers(connection),
                PostgresClients(connection),
                PostgresChallenges(connection),
                PostgresProofs(connection),
                PostgresConsents(connection),
                PostgresTokens(connection),
                PostgresPayments(connection),
                PostgresLedger(connection, self._new_id),
                PostgresIdempotencyKeys(connection),
            )
```

Opens a unit of work on a pooled connection. `@contextmanager` on `__call__` turns the method into a `with`-block provider: `with self._pool.connection() as connection:` borrows a connection and starts a transaction; the generator `yield`s a `PostgresUnitOfWork`; when the body ends normally the pool commits, when it raises the pool rolls back, and the connection returns to the pool.

```python
class PostgresHealth:
    """`HealthProbe`: can a pooled connection run `SELECT 1`?"""

    def __init__(self, pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
        self._pool = pool

    def check(self) -> None:
        """Return normally when healthy; raise when not."""
        with self._pool.connection(timeout=2.0) as connection:
            connection.execute("SELECT 1")
```

The health probe: borrow a connection (waiting at most 2 seconds) and run `SELECT 1`. If anything raises, the health check reports the failure.

## Try it yourself

1. Run the unit tests of these adapters (no database needed):

   ```bash
   .venv/bin/python -m pytest tests/unit/infrastructure -q
   ```

   Expected: all pass.

2. Make a key pair and sign something:

   ```bash
   .venv/bin/python -c "
   from open_banking.infrastructure import keys
   pem, jwk = keys.generate_keypair()
   signature = keys.sign(pem, b'hello')
   print(keys.verify(jwk, b'hello', signature), keys.verify(jwk, b'hellO', signature))"
   ```

   Expected: `True False`.

3. Hash a secret and check it:

   ```bash
   .venv/bin/python -c "
   from open_banking.infrastructure.crypto import StandardCrypto
   c = StandardCrypto(pepper='x' * 16)
   stored = c.hash_secret('1234')
   print(stored.split('\$')[0], c.verify_secret('1234', stored), c.verify_secret('9999', stored))"
   ```

   Expected: `scrypt True False`.

## Mistakes people make

- **Storing a PIN, a code or a token.** Only slow salted hashes or keyed digests are stored.
- **Comparing secrets with `==`.** `hmac.compare_digest` takes the same time wherever the values differ.
- **Accepting any signing algorithm named in a token.** The code accepts only `ES256` and type `dpop+jwt`.
- **Writing a private key and then restricting it.** The file is given permission `0o600` before the key is written.
- **A different idempotency key on each retry.** The merchant signs and sends the same key every time.

## Check yourself

1. What does a pepper do that a salt does not? *It is a secret kept outside the database, so a stolen digest cannot be attacked offline without it.*
2. What does a DPoP proof contain? *A unique id, the HTTP method, the URL, the issue time and, with a token, the token's hash, all signed by the key.*
3. How does the database detect a replayed proof? *`INSERT ... ON CONFLICT DO NOTHING RETURNING`: a first use returns a row, a repeat returns none.*
4. Why does `DROP_RESPONSE` matter? *It reproduces a payment that was made while the merchant never saw the answer.*
5. What is the backoff between attempts? *The base delay times 2 to the power of the attempt number minus one.*

## Further reading

- PyJWT documentation: https://pyjwt.readthedocs.io/
- `cryptography` documentation: https://cryptography.io/en/latest/
- Python 3.12 `hashlib.scrypt`: https://docs.python.org/3.12/library/hashlib.html#hashlib.scrypt
- Python 3.12 `hmac`: https://docs.python.org/3.12/library/hmac.html
- RFC 7638, JSON Web Key Thumbprint: https://www.rfc-editor.org/rfc/rfc7638
- RFC 9449, DPoP: https://www.rfc-editor.org/rfc/rfc9449
- psycopg 3 documentation: https://www.psycopg.org/psycopg3/docs/
