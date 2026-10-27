# JWT, PyJWT and DPoP (proof of possession)

## What it is

A **JWT** (JSON Web Token) is a small, signed piece of text with three parts joined by dots: `header.payload.signature`. The header and payload are JSON, written in a text-safe encoding called base64url. The signature lets the receiver check that the header and payload were not changed and who made them. **PyJWT** is the Python library (`import jwt`) that builds and checks JWTs.

**DPoP** (Demonstrating Proof of Possession, a standard published as RFC 9449) is a way to make a stolen access token useless. The client owns a private key. With every request it sends a fresh, signed JWT, the **proof**, that says "I hold the key, and I made this proof for exactly this request, now". The bank accepts a token only together with such a proof from the key the token was issued to.

## Why this repository uses it

The lesson is about open banking: a merchant asks a bank to move a customer's money. If the merchant's access token leaked, anyone could use it. Binding the token to the merchant's key means a thief with only the token cannot make a valid request. DPoP also gives replay protection: each proof has a unique id (`jti`) that the bank remembers, so a captured request cannot be sent again.

## Where it appears

| Path | What it does |
| --- | --- |
| `src/open_banking/infrastructure/keys.py` | `build_dpop_proof()` (client side) and `sign()` / `verify()` for ES256 signatures; `thumbprint()`, `normalize_url()`, `access_token_hash()`. |
| `src/open_banking/infrastructure/crypto.py` | `StandardCrypto.verify_dpop_proof()`: the bank's checks of a proof. |
| `src/open_banking/application/bank/access.py` | `IssueToken` and `AuthenticateCaller`: where the proof is demanded and its `jti` remembered. |
| `src/open_banking/infrastructure/bank_client.py` | The merchant's client builds a proof for the token request and for every payment request. |
| `src/open_banking/interfaces/bank_api.py` | Reads the `Authorization: DPoP <token>` and `DPoP: <proof>` headers. |
| `pyproject.toml`, `requirements.lock` | `pyjwt[crypto]>=2.9,<3`; pinned `pyjwt==2.15.1`. |
| `sql/001_schema.sql` | Tables `access_tokens` and `seen_proofs`. |

## How it works underneath

1. The merchant is registered once: the bank stores its **public key** and a hash of its secret. The private key never leaves the merchant.
2. To get a token the merchant sends its client id and secret **and** a proof signed with its private key, for `POST /oauth/token`. The bank checks the secret, checks the proof against the stored public key, and issues a random token, remembering the key's **thumbprint** (a short fingerprint of the public key) next to it. The token is returned with `token_type: DPoP`.
3. For each payment the merchant sends `Authorization: DPoP <token>` and a new proof for `POST /v1/payments`, containing a hash of the token (`ath`). The bank looks up the token, checks that the proof's key matches the token's thumbprint, and checks the proof says this method, this URL and a recent time.
4. The bank stores each proof's `jti`. A second proof with a seen `jti` is refused.

## Walkthrough of this repository's use

### The shape of a proof

A proof is a JWT whose header is `{"typ": "dpop+jwt", "alg": "ES256", "jwk": {public key}}` and whose payload is:

| Claim | Meaning |
| --- | --- |
| `jti` | A fresh unique id (`uuid4().hex`). |
| `htm` | The HTTP method, upper case (`POST`). |
| `htu` | The URL, without query and fragment. |
| `iat` | When it was issued, in whole seconds since 1970 (a Unix time). |
| `ath` | Only on requests that carry a token: base64url of the SHA-256 of the token. |

### Building it (the merchant)

```python
claims: dict[str, Any] = {
    "jti": uuid.uuid4().hex,
    "htm": method.upper(),
    "htu": normalize_url(url),
    "iat": int(now.timestamp()),
}
if access_token is not None:
    claims["ath"] = access_token_hash(access_token)
encoded: str = jwt.encode(claims, private_pem, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": public_jwk})
```

`jwt.encode(payload, key, algorithm=..., headers=...)` signs the payload with the private key (ES256, see `cryptography.md`) and adds the extra header fields. `normalize_url` keeps only scheme, host and path (`urlsplit` / `urlunsplit`), so a proof is bound to `http://localhost:8463/v1/payments`, not to a query string.

### Checking it (the bank)

```python
header = jwt.get_unverified_header(proof)
if header.get("typ") != "dpop+jwt" or header.get("alg") != "ES256":
    raise ClientAuthenticationError("invalid merchant credential")
embedded = header.get("jwk")
if embedded is not None and self.thumbprint(embedded) != self.thumbprint(public_jwk):
    raise ClientAuthenticationError("the proof was signed with a different key")
key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(dict(public_jwk)))
claims = jwt.decode(proof, key, algorithms=["ES256"], options={"verify_exp": False, "verify_nbf": False, "verify_iat": False, "verify_aud": False})
```

- `get_unverified_header` reads the header **without** trusting it. Only the type and algorithm are inspected, and both must be exactly what is expected: accepting whatever algorithm the sender names is a classic JWT vulnerability.
- Even if the proof carries its own `jwk`, the bank verifies against the key **it stored** for this merchant, never against a key the sender supplies.
- `ECAlgorithm.from_jwk` turns a JWK (a JSON description of a public key) into a key object. `jwt.decode(..., algorithms=["ES256"])` checks the signature and returns the claims. The `options` switch off PyJWT's automatic checks of `exp`, `nbf`, `iat` and `aud`, because DPoP proofs do not use those claims in the usual way; the bank does its own checks next.

```python
if claims.get("htm") != method.upper() or claims.get("htu") != keys.normalize_url(url): ...
if abs(now - issued_at) > self._max_skew: ...          # default 60 seconds
if access_token is not None and claims.get("ath") != keys.access_token_hash(access_token): ...
if access_token is None and "ath" in claims: ...
jti = claims.get("jti")
```

Each check has its own message: made for a different request; too old or from the future; does not belong to this token; names a token the request does not carry. The `jti` is then stored with `remember_proof`, which fails if it was seen before (a primary key in the `seen_proofs` table), so **a replayed proof is refused**.

### Why the error text is vague

Failures about credentials mostly say only `invalid merchant credential`. The bank must not tell an attacker which part was wrong.

### The bank also signs

Payment results carry a `Bank-Signature` header: an ES256 signature, made with the bank's own key, over the exact JSON body (sorted keys, no spaces). The merchant fetches the public key from `/.well-known/bank-keys.json` and verifies it, so it knows the result really came from the bank.

## Try it yourself

1. Build and inspect a proof (from the repository root):

   ```bash
   .venv/bin/python - <<'EOF'
   import jwt
   from datetime import UTC, datetime
   from open_banking.infrastructure import keys
   pem, jwk = keys.generate_keypair()
   proof = keys.build_dpop_proof(pem, jwk, method="post", url="http://localhost:8463/v1/payments?x=1",
                                 now=datetime(2026, 10, 27, 12, 0, tzinfo=UTC), access_token="tok")
   print(proof.count("."), sorted(jwt.get_unverified_header(proof)))
   claims = jwt.decode(proof, options={"verify_signature": False})
   print(sorted(claims), claims["htm"], claims["htu"])
   EOF
   ```

   Expected: `2 ['alg', 'jwk', 'typ']`, then `['ath', 'htm', 'htu', 'iat', 'jti'] POST http://localhost:8463/v1/payments`. (The proof has two dots, three parts; the method became upper case and the query string was dropped. `verify_signature: False` is used here only to look inside.)

2. Run the tests that exercise it: `.venv/bin/python -m pytest tests/unit -q -k "proof or dpop or token"`.

3. With the demo (`make up`, `make run`) look for the scenario sections on a replayed proof and on a stolen token.

## Mistakes people make

- **Trusting the header.** Never take the algorithm or the key from the token being checked. This code fixes `ES256` and uses the stored key.
- **Treating a JWT as encrypted.** It is signed, not secret: anyone can read the payload (base64url is not encryption).
- **Leaving `exp` unchecked in a normal JWT.** DPoP proofs use `iat` with a small skew window instead, checked by hand here.
- **Not remembering `jti`.** Without it a captured proof can be replayed within the window.
- **Binding a proof to the wrong URL.** The bank must compare against the URL the merchant used (`BANK_PUBLIC_URL`), not whatever internal address received it.

## Check yourself

1. What are the three parts of a JWT? *Header, payload and signature, joined by dots.*
2. What does DPoP add to an access token? *The token only works together with a fresh proof signed by the key the token was issued to.*
3. Why does the bank remember each proof's `jti`? *So a captured proof cannot be replayed.*
4. What does `ath` contain? *The hash of the access token the proof accompanies.*
5. Why does the bank ignore the `jwk` inside the proof when verifying? *It verifies against the public key it stored at registration; a sender-supplied key proves nothing.*

## Further reading

- PyJWT 2.15 documentation: https://pyjwt.readthedocs.io/en/stable/
- PyJWT usage (encoding, ES256, headers): https://pyjwt.readthedocs.io/en/stable/usage.html
- JSON Web Token (RFC 7519): https://www.rfc-editor.org/rfc/rfc7519
- JSON Web Signature (RFC 7515) and Algorithms (RFC 7518, ES256): https://www.rfc-editor.org/rfc/rfc7515, https://www.rfc-editor.org/rfc/rfc7518
- JWK Thumbprint (RFC 7638): https://www.rfc-editor.org/rfc/rfc7638
- DPoP (RFC 9449): https://www.rfc-editor.org/rfc/rfc9449
