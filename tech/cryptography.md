# The cryptography package (elliptic-curve keys)

## What it is

`cryptography` is the standard Python library for cryptographic building blocks, written mostly in Rust and linked to OpenSSL. This repository uses a small part of it: creating and storing **elliptic-curve key pairs**. A key pair is a **private key** (kept secret, used to sign) and a **public key** (shared, used to check signatures). A digital **signature** proves that whoever holds the private key approved exactly these bytes.

## Why this repository uses it

Two parties sign things in the lesson. The **merchant** signs every payment request and every proof of possession; the **bank** signs every payment result. Neither signature can be forged without the private key, and the other side only needs the public key to check it. The `cryptography` package creates the keys and PyJWT (see `jwt-and-dpop.md`) uses it underneath to make and check the actual ES256 signatures.

## Where it appears

| Path | What it does |
| --- | --- |
| `src/open_banking/infrastructure/keys.py` | `generate_keypair()`, `public_jwk_of()`, `sign()`, `verify()`, `write_private_key()`, `thumbprint()`. |
| `src/open_banking/infrastructure/crypto.py` | Checks that a key loaded from a JWK is an elliptic-curve public key. |
| `src/open_banking/interfaces/admin_cli.py` | Calls `generate_keypair()` when a merchant is registered; writes the private key to a file readable by its owner only. |
| `pyproject.toml` | `pyjwt[crypto]` brings `cryptography` in; the import-linter contract forbids `cryptography` and `jwt` in `domain` and `application`. |
| `requirements.lock` | `cryptography==50.0.2` and `cffi==2.1.1`. |

## How it works underneath

- **Elliptic-curve cryptography.** A private key is a large secret number. The public key is a point on a mathematical curve computed from it. Going from private to public is easy; going back is believed to be infeasible. The curve used here is **P-256** (also called `secp256r1` or `prime256v1`).
- **ECDSA with SHA-256 = "ES256".** To sign, hash the message with SHA-256 and combine the hash with the private key and a random number to produce two integers, **R** and **S**. Verification takes the message, the signature and the public key, and answers yes or no.
- **Formats.** A private key is stored as **PEM**: base64 text between `-----BEGIN PRIVATE KEY-----` and `-----END PRIVATE KEY-----` lines (here in PKCS#8 layout, the standard container for private keys). A public key is shared as a **JWK** (JSON Web Key): a JSON object with `kty` (`EC`), `crv` (`P-256`) and the two coordinates `x` and `y`, base64url-encoded.
- **Keys as objects.** `cryptography` represents them as Python objects, such as `EllipticCurvePrivateKey`.

## Walkthrough of this repository's use

### Making a key pair

```python
def generate_keypair() -> tuple[str, dict[str, Any]]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, public_jwk_of(pem)
```

- `ec.generate_private_key(ec.SECP256R1())` draws a new random private key on the P-256 curve from the operating system's secure random source.
- `private_bytes(...)` serializes it: `Encoding.PEM` (text), `PrivateFormat.PKCS8` (the standard layout), `NoEncryption()` (no passphrase on the file; protection is the file's permissions). `.decode()` turns bytes into a string.
- `public_jwk_of(pem)` computes the public half as a JWK.

```python
private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)
assert isinstance(private_key, ec.EllipticCurvePrivateKey)
jwk = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
return {key: jwk[key] for key in ("kty", "crv", "x", "y")}
```

`load_pem_private_key` parses PEM text back into an object (it may return several key types, so the `assert isinstance` narrows it for mypy and rejects a wrong kind of key). `ECAlgorithm.to_jwk` (from PyJWT) converts the public key to JSON. The dict comprehension keeps only the four members that the thumbprint rule uses, so private members can never leak into a JWK.

### Saving it safely

```python
path.parent.mkdir(parents=True, exist_ok=True)
path.touch(mode=0o600, exist_ok=True)
path.chmod(0o600)
path.write_text(pem, encoding="utf-8")
```

`0o600` is a file permission written in octal: the owner may read and write; no one else may do anything. The file is created with that mode **before** the secret is written into it, so there is no moment when the key sits in a world-readable file.

### Signing and verifying

```python
_ALGORITHM = ECAlgorithm(ECAlgorithm.SHA256)

def sign(private_pem: str, message: bytes) -> str:
    key = _ALGORITHM.prepare_key(private_pem)
    return b64url(_ALGORITHM.sign(message, key))

def verify(public_jwk: dict[str, Any], message: bytes, signature: str) -> bool:
    try:
        key = ECAlgorithm.from_jwk(json.dumps(public_jwk))
        return bool(_ALGORITHM.verify(message, key, b64url_decode(signature)))
    except (ValueError, TypeError, jwt.PyJWTError):
        return False
```

- PyJWT's `ECAlgorithm(SHA256)` is ES256. `sign` returns the signature as the raw 64 bytes **R||S** (32 bytes each), base64url-encoded, the form JOSE standards use (not the DER form OpenSSL prints).
- `verify` never raises on bad input: a malformed key or signature simply returns `False`.
- What gets signed: for a payment request, the bytes of a canonical JSON document naming the client, consent, payee, amount, currency and idempotency key (`domain/signing.py`). Change any field and the signature no longer matches; the bank then refuses the payment with `request_not_as_signed`.

### Thumbprints

```python
members = {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]}
return b64url(hashlib.sha256(json.dumps(members, sort_keys=True, separators=(",", ":")).encode()).digest())
```

The **thumbprint** (RFC 7638) is a short, canonical fingerprint of a public key: SHA-256 of the key's four members as sorted compact JSON, base64url-encoded (43 characters). The bank stores the thumbprint of the merchant's key beside each access token and compares it to the key that signs each proof.

## Try it yourself

1. Make a key pair and look at it:

   ```bash
   .venv/bin/python - <<'EOF'
   from open_banking.infrastructure import keys
   pem, jwk = keys.generate_keypair()
   print(pem.splitlines()[0])
   print(sorted(jwk), jwk["kty"], jwk["crv"], len(keys.thumbprint(jwk)))
   EOF
   ```

   Expected: `-----BEGIN PRIVATE KEY-----`, then `['crv', 'kty', 'x', 'y'] EC P-256 43`.

2. Sign and verify, then tamper:

   ```bash
   .venv/bin/python - <<'EOF'
   from open_banking.infrastructure import keys
   pem, jwk = keys.generate_keypair()
   signature = keys.sign(pem, b"hello")
   print(len(keys.b64url_decode(signature)), keys.verify(jwk, b"hello", signature), keys.verify(jwk, b"hellp", signature))
   EOF
   ```

   Expected: `64 True False`.

3. Check a saved key's permissions (after `make register`): `ls -l var/merchant-1-key.pem`. Expected: `-rw-------` (owner only).

4. See the version: `.venv/bin/pip show cryptography | grep -i "^version"`. Expected: `Version: 50.0.2`.

## Mistakes people make

- **Rolling your own cryptography.** This repository only calls established functions.
- **Writing a private key with default permissions** and tightening them afterwards. Create it with the restrictive mode first.
- **Storing private keys in the repository.** Keys live under `var/`, which `.gitignore` excludes, and the bank's key in a volume.
- **Accepting any key type.** The `assert isinstance(..., ec.EllipticCurvePrivateKey)` checks refuse an RSA or other key.
- **Comparing signatures with `==`.** Use the library's verify function.

## Check yourself

1. Which key signs and which verifies? *The private key signs; the public key verifies.*
2. What does ES256 mean? *ECDSA on the P-256 curve with SHA-256.*
3. Why create the key file with mode `0o600` before writing to it? *So the secret is never in a file other users can read, even briefly.*
4. What is a JWK thumbprint for? *A short canonical fingerprint to compare public keys.*
5. Why does `verify` return `False` instead of raising on a malformed signature? *Callers treat any failure as "not valid" without handling library exceptions.*

## Further reading

- cryptography documentation (50.x): https://cryptography.io/en/latest/
- Elliptic-curve primitives: https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ec/
- Key serialization (PEM, PKCS#8): https://cryptography.io/en/latest/hazmat/primitives/asymmetric/serialization/
- JSON Web Key (RFC 7517): https://www.rfc-editor.org/rfc/rfc7517
- ECDSA for JOSE, ES256 (RFC 7518, section 3.4): https://www.rfc-editor.org/rfc/rfc7518#section-3.4
