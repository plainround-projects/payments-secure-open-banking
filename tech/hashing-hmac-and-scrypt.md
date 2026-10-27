# Hashing, HMAC, scrypt and secure random numbers (Python standard library)

## What it is

Four small tools from Python's standard library (`hashlib`, `hmac`, `secrets`, `os.urandom`) that handle secrets safely.

- A **hash function** such as SHA-256 turns any input into a fixed-size fingerprint. The same input always gives the same fingerprint; you cannot get the input back; changing one bit of the input changes the whole fingerprint.
- **HMAC** (hash-based message authentication code) is a hash that is also **keyed**: the result depends on the data and on a secret key, so only someone with the key can compute or check it.
- **scrypt** is a deliberately **slow, memory-hungry** hash used for passwords and PINs, so that guessing millions of candidates is expensive.
- **Secure random numbers** (`secrets`, `os.urandom`) come from the operating system's cryptographic generator, unlike `random`, which is predictable.

## Why this repository uses it

A bank must never store a customer's PIN, a merchant's client secret, a one-time code or an access token in a form that a database leak would turn into working credentials. The lesson's rules are: store **hashes**, store them in a way that is expensive to attack, tie each digest to its purpose, and compare in constant time. This is the part of the system the "security" half of the lesson is graded on.

## Where it appears

| Path | What it does |
| --- | --- |
| `src/open_banking/infrastructure/crypto.py` | `hash_secret`, `verify_secret` (scrypt), `code_digest`, `token_digest` (HMAC-SHA256 with a pepper), `new_token`, `new_code`. |
| `src/open_banking/domain/consent.py` | `ConsentTerms.digest()`: SHA-256 over the consent terms. |
| `src/open_banking/domain/idempotency.py` | `fingerprint()`: SHA-256 over an operation and its fields. |
| `src/open_banking/infrastructure/keys.py` | SHA-256 for key thumbprints and the `ath` token hash. |
| `src/open_banking/interfaces/admin_cli.py` | `secrets.token_urlsafe(32)` for client secrets; `secrets.randbelow(10**6)` for PINs. |
| `src/open_banking/infrastructure/bank_settings.py` | `BANK_PEPPER` (at least 16 characters) and `SCRYPT_N` (a power of two, default 16384). |
| `.env.example`, `Makefile` target `env` | The pepper is generated with `openssl rand` and never committed. |

## How it works underneath

- A hash function has no key and no secret. Anyone can compute SHA-256 of a guess. So a hash alone only protects data that is already unguessable (a 256-bit random token). A six-digit code has only a million possibilities, so its plain hash could be reversed in a blink by trying them all.
- A **salt** is random data stored beside a password hash and mixed into the computation, so two identical secrets get different hashes and precomputed tables are useless.
- A **pepper** is a secret that is **not** stored in the database, kept in the server's configuration. Even with a copy of the database, an attacker cannot compute the digests without it.
- **Constant-time comparison** compares two values in a time that does not depend on where they first differ, so an attacker cannot learn a secret byte by byte from response times.

## Walkthrough of this repository's use

### Slow salted hashes for PINs and client secrets

```python
def hash_secret(self, secret: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=self._scrypt_n, r=8, p=1, dklen=32)
    return f"scrypt${self._scrypt_n}$8$1${keys.b64url(salt)}${keys.b64url(digest)}"
```

- `os.urandom(16)` draws 16 random bytes for the salt.
- `hashlib.scrypt(password, salt=, n=, r=, p=, dklen=)`: `n` is the CPU/memory cost (a power of two; 16384 by default), `r` the block size (8) and `p` the parallelism (1). `dklen=32` asks for a 32-byte result.
- The stored string is `scrypt$n$r$p$salt$hash`: the **parameters travel with the hash**, so the cost can be raised later and old hashes still verify.

```python
def verify_secret(self, secret: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(secret.encode(), salt=keys.b64url_decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(digest, keys.b64url_decode(expected))
    except (ValueError, TypeError):
        return False
```

It recomputes with the stored salt and parameters and compares with `hmac.compare_digest`, the constant-time comparison. Any malformed stored value returns `False` instead of raising.

A related precaution is in `IssueToken`: when the client id is unknown the bank still verifies the supplied secret against a pre-computed **decoy hash**. Otherwise the request would return faster for unknown clients than for known ones, telling an attacker which client ids exist.

### Keyed digests for codes and tokens

```python
def code_digest(self, challenge_id: str, code: str) -> str:
    return hmac.new(self._pepper, f"code:{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()

def token_digest(self, token: str) -> str:
    return hmac.new(self._pepper, f"token:{token}".encode(), hashlib.sha256).hexdigest()
```

- `hmac.new(key, message, digestmod)` computes HMAC-SHA256 with the pepper as the key. `.hexdigest()` gives 64 hex characters.
- The message is prefixed (`code:` / `token:`) and, for codes, includes the **challenge id**. A digest made for one purpose cannot be reused as another, and a code digest cannot be attacked offline without the pepper (which is why a plain SHA-256 of a six-digit code would not do).
- Access tokens are random 256-bit values, so a fast keyed hash is enough; only the digest is stored in the `access_tokens` table.

### Random values

```python
self._code_source = code_source or (lambda: f"{secrets.randbelow(10**6):06d}")
def new_token(self) -> str:
    return secrets.token_urlsafe(32)
```

`secrets.randbelow(10**6)` is a uniform random integer below one million; `:06d` pads it to six digits. `secrets.token_urlsafe(32)` returns 32 random bytes as 43 URL-safe characters. The one-time-code source can be replaced in tests so they know the code; production always uses `secrets`.

### SHA-256 for fingerprints (no secret involved)

`ConsentTerms.digest()` hashes the consent terms (sorted compact JSON). A proof of authentication carries that digest, so the customer's approval of "50.00 to the shop" cannot be used to create a consent for 5,000.00: the digests differ ("dynamic linking"). `fingerprint()` hashes a payment request so a reused idempotency key with a different amount is detected. These are public values; integrity is the goal, not secrecy.

### The pepper

`BANK_PEPPER` must be at least 16 characters (`BankConfig.from_env` refuses less). `make up` generates a random 48-character one into `.env`; CI uses a throwaway value; it is never committed.

### `SCRYPT_N` in tests

The default cost makes each PIN check take noticeable time, which is the point in production but slows hundreds of tests. The unit and integration tests build `StandardCrypto(..., scrypt_n=16)` directly, and CI sets `SCRYPT_N=1024` for the real server processes of the end-to-end tests. The environment setting is validated: a power of two, 2 or more.

## Try it yourself

1. A salted slow hash and its verification:

   ```bash
   .venv/bin/python - <<'EOF'
   from open_banking.infrastructure.crypto import StandardCrypto
   crypto = StandardCrypto(pepper="a-demo-pepper-16+", scrypt_n=1024)
   stored = crypto.hash_secret("123456")
   print(stored.split("$")[:4], len(stored.split("$")))
   print(crypto.verify_secret("123456", stored), crypto.verify_secret("000000", stored))
   print(crypto.hash_secret("123456") == stored)
   EOF
   ```

   Expected: `['scrypt', '1024', '8', '1'] 6`, then `True False`, then `False` (the same secret hashes differently every time because of the salt).

2. Keyed digests depend on the key:

   ```bash
   .venv/bin/python - <<'EOF'
   from open_banking.infrastructure.crypto import StandardCrypto
   a = StandardCrypto(pepper="pepper-number-one!!", scrypt_n=1024)
   b = StandardCrypto(pepper="pepper-number-two!!!", scrypt_n=1024)
   print(len(a.code_digest("ch1", "123456")), a.code_digest("ch1", "123456") == b.code_digest("ch1", "123456"))
   EOF
   ```

   Expected: `64 False`.

3. Random values: `.venv/bin/python -c "import secrets; print(len(secrets.token_urlsafe(32)), secrets.randbelow(10**6))"`. Expected: `43` and a number below a million.

4. Run the test about a wrong pepper or a stolen database: `.venv/bin/python -m pytest tests/unit -q -k "crypto or digest or hash"`.

## Mistakes people make

- **Plain SHA-256 for passwords or short codes.** Too fast; use scrypt, or a keyed digest with a secret the attacker lacks.
- **Comparing secrets with `==`.** Use `hmac.compare_digest`.
- **Using `random` for secrets.** It is not secure; use `secrets` or `os.urandom`.
- **Storing the pepper in the database** or in git. Then it protects nothing.
- **Forgetting that the parameters must be stored with the hash**, which makes raising the cost later impossible.
- **Letting "unknown user" return faster than "wrong secret".** The decoy hash equalizes the time.

## Check yourself

1. Why is a plain hash of a six-digit code weak? *There are only a million codes; an attacker can hash them all.*
2. What does a salt do? *Makes identical secrets hash differently and defeats precomputed tables.*
3. What is a pepper and where is it kept? *A secret key mixed into digests; it lives in the server's configuration, not the database.*
4. Why does the stored scrypt string include `n`, `r` and `p`? *So the cost can be changed later and old hashes still verify.*
5. Why compare with `hmac.compare_digest`? *Its running time does not reveal how many leading bytes matched.*

## Further reading

- `hashlib` (including `scrypt`): https://docs.python.org/3.12/library/hashlib.html
- `hmac`: https://docs.python.org/3.12/library/hmac.html
- `secrets`: https://docs.python.org/3.12/library/secrets.html
- scrypt (RFC 7914): https://www.rfc-editor.org/rfc/rfc7914
- HMAC (RFC 2104): https://www.rfc-editor.org/rfc/rfc2104
- OWASP password storage cheat sheet: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
