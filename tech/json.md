# JSON

## What it is

JSON (JavaScript Object Notation) is a text format for structured data. It has objects (`{"key": value}`), arrays (`[1, 2]`), strings, numbers, `true`/`false` and `null`. It is the common language of web APIs, and it is also the inside of several security formats: a JWT's header and payload are JSON, and a public key is shared as a JWK, a JSON object.

## Why this repository uses it

Four jobs: request and response bodies of both services; the **canonical form** that gets signed and hashed (the same data must always give the same bytes, or signatures and fingerprints would not match); the **JWK** description of public keys; and PostgreSQL `jsonb` columns that store a merchant's public key and the stored answer for an idempotency key.

## Where it appears

| Path | Role |
| --- | --- |
| `src/.../interfaces/bank_api.py`, `merchant_api.py` | `JSONResponse(...)` for every response. |
| `src/.../domain/signing.py` | `canonical_payment_request()`: the bytes the merchant signs. |
| `src/.../domain/consent.py` | `ConsentTerms.digest()`: SHA-256 of canonical JSON of the terms. |
| `src/.../domain/idempotency.py` | `fingerprint()`: SHA-256 of canonical JSON of a request. |
| `src/.../infrastructure/keys.py` | JWKs, the thumbprint, JWT payloads. |
| `src/.../infrastructure/otp.py` | The outbox file holds one JSON object per line. |
| `sql/001_schema.sql` | `oauth_clients.public_jwk JSONB`, `idempotency_keys.response_body JSONB`. |

## How it works underneath

On the wire JSON is just text: the sender **serializes** a value to text and the receiver **parses** it back (`json.dumps` / `json.loads` in Python). A Python dict becomes an object, a list an array, `None` becomes `null`. Two JSON texts can mean the same thing yet differ as bytes (key order, spaces). A signature or a hash is over bytes, so both sides must produce exactly the same bytes: a **canonical form**.

## Walkthrough of this repository's use

### The canonical form that gets signed

```python
def canonical_payment_request(*, client_id, consent_id, payee_account, amount, idempotency_key) -> bytes:
    document = {
        "amount_minor": amount.minor,
        "client_id": client_id,
        "consent_id": consent_id,
        "currency": amount.currency,
        "idempotency_key": idempotency_key,
        "payee_account": payee_account,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
```

- `sort_keys=True` orders keys alphabetically; `separators=(",", ":")` removes all spaces; `.encode()` makes bytes (UTF-8).
- The merchant signs these bytes; the bank rebuilds them from the request it received and verifies. If **any** field differs (a bigger amount, another payee) the signatures do not match and the bank refuses with `request_not_as_signed`. The idempotency key is inside, so a signature cannot be moved onto another attempt.

### Canonical JSON for digests

`ConsentTerms.digest()` and `fingerprint()` use the same two options and then `hashlib.sha256(...).hexdigest()`. The first ties a customer's authentication proof to exact terms; the second tells a retry of a payment (same fingerprint) from a different request that reuses the key.

### A public key as JSON (JWK)

```json
{"kty": "EC", "crv": "P-256", "x": "<base64url>", "y": "<base64url>"}
```

A **JWK** (JSON Web Key) describes a public key: the key type (`EC`), the curve (`P-256`) and the two coordinates. The bank stores the merchant's JWK, serves its own at `/.well-known/bank-keys.json`, and computes a **thumbprint** by hashing just these four members in canonical form.

### JSON inside the database

`jsonb` stores parsed JSON in a binary form. The merchant's `public_jwk` is stored and read back as a Python dict; the stored response of an idempotency key is written with `Jsonb(dict)` (see `psycopg.md`) and replayed exactly. `jsonb` does not keep key order or spaces, so a replay returns equal content, not equal bytes. That is why signatures are made over a canonical form of the body, not over the stored bytes.

### Lines of JSON in a file

```python
record = {"sent_at": ..., "customer_id": ..., "code": code, "purpose": purpose}
outbox.write(json.dumps(record) + "\n")
```

The file that stands in for the customer's phone holds one JSON object per line (JSON Lines). Appending a line is simple and safe, and each line parses on its own. It is the **only** place a one-time code is ever written in clear; the file is created readable by its owner only.

### One error shape

`{"error": {"code": "amount_exceeds_consent", "message": "amount 60.00 exceeds consent max 50.00"}}`, sometimes with extra fields (`attempts_left` after a wrong code, `retryable` on the merchant side).

### What JSON cannot hold

No dates (ISO-8601 strings such as `"2026-10-27T12:00:00+00:00"`), no exact decimals (hence integer `amount_minor` plus a string `amount`), no comments.

## Try it yourself

1. The canonical form:

   ```bash
   .venv/bin/python - <<'EOF'
   from open_banking.domain.money import Money
   from open_banking.domain.signing import canonical_payment_request
   print(canonical_payment_request(client_id="merchant-1", consent_id="c-1", payee_account="acct-shop",
                                   amount=Money(3000, "USD"), idempotency_key="k-1").decode())
   EOF
   ```

   Expected: `{"amount_minor":3000,"client_id":"merchant-1","consent_id":"c-1","currency":"USD","idempotency_key":"k-1","payee_account":"acct-shop"}`.

2. Changing one field changes the digest:

   ```bash
   .venv/bin/python - <<'EOF'
   from open_banking.domain.idempotency import fingerprint
   print(fingerprint("initiate-payment", {"amount_minor": 3000}) == fingerprint("initiate-payment", {"amount_minor": 3001}))
   EOF
   ```

   Expected: `False`.

3. After `make register`, look at the merchant's credential file keys (not the secret): `cut -d= -f1 var/merchant-1.env`. Expected: `MERCHANT_CLIENT_ID`, `MERCHANT_CLIENT_SECRET`, `MERCHANT_PRIVATE_KEY_FILE`.

## Mistakes people make

- **Signing "the JSON" without a canonical form.** Two correct implementations would produce different bytes.
- **Signing the parsed object after re-serializing with default settings.** Spaces and order change.
- **Floats for money.** Use integers and strings.
- **Putting a code or secret in a JSON log line.** The one outbox file is the exception, and it is owner-only.
- **Assuming `jsonb` preserves the original text.** It does not.

## Check yourself

1. Why does the signed document use `sort_keys=True`? *So both sides produce identical bytes.*
2. What is a JWK? *A JSON description of a public key.*
3. Why is the idempotency key part of the signed request? *So a signature cannot be reused for a different attempt.*
4. What does `jsonb` not keep? *Key order and spacing.*
5. What is JSON Lines? *One JSON object per line in a text file.*

## Further reading

- JSON (ECMA-404): https://ecma-international.org/publications-and-standards/standards/ecma-404/
- Python `json`: https://docs.python.org/3.12/library/json.html
- JSON Web Key (RFC 7517): https://www.rfc-editor.org/rfc/rfc7517
- PostgreSQL 18, JSON types: https://www.postgresql.org/docs/18/datatype-json.html
- JSON Lines: https://jsonlines.org/
