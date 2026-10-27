# Python

## What it is

Python is a programming language. You write instructions in text files that end in `.py`, and a program called the **interpreter** (`python`) reads them and carries them out one by one. It is the language every file under `src/` and `tests/` in this repository is written in.

A **module** is one `.py` file. A **package** is a folder of modules with an `__init__.py` file in it. Here `src/open_banking/` is the package, and `domain`, `application`, `infrastructure` and `interfaces` are sub-packages.

## Why this repository uses it

The lesson builds a small open-banking system: a made-up bank, a made-up merchant, strong customer authentication, consent as an object and a payment that happens at most once. Python keeps the two services and the security code readable, and its type hints let a checker (mypy) prove that, for example, a consent can never be created without the proof of authentication it needs. The rules (consent, authentication, money, the signed request) are in the `domain` package and use only the standard library.

## Where it appears

| Path | What it does with Python |
| --- | --- |
| `src/open_banking/domain/` | Pure rules: `Money`, `ConsentTerms` and `Consent`, `Challenge`, `Transfer`, the signed request, the idempotency key. Standard library only. |
| `src/open_banking/application/bank/` | The bank's use cases (start a challenge, verify a code, grant and revoke a consent, issue a token, initiate a payment) and the ports they need. |
| `src/open_banking/application/merchant/` | The merchant's checkout. It cannot import anything from the bank side (an import-linter contract enforces it). |
| `src/open_banking/infrastructure/` | PostgreSQL, cryptography, the one-time-code outbox, the bank client, settings, faults, logging. |
| `src/open_banking/interfaces/` | The two HTTP applications, the two server launchers, the admin, customer, demo and health programs. |
| `tests/` | The tests, also Python. |
| `pyproject.toml` | `requires-python = ">=3.12"`. |
| `Dockerfile` | Builds on the image `python:3.12.8-slim-bookworm`. |

## How it works underneath

1. You start `python -m some.module`. The interpreter finds the module on its **import path**, a list of folders.
2. It compiles the text to **bytecode** (a compact list of simple steps) and runs it on a small virtual machine. In memory, every value is an **object** with a type.
3. `import x` runs module `x` once and keeps the result in a dictionary called `sys.modules`.
4. **Type hints** such as `amount: Money` are notes the interpreter ignores; mypy reads them (see `mypy.md`).
5. Python has a **global interpreter lock** (GIL): only one thread runs Python bytecode at a time. Threads still help when they wait on the database or the network.

## Walkthrough of this repository's use

### The file header

```python
from __future__ import annotations
```

At the top of almost every module: type hints are stored as text instead of being evaluated when the module loads, so a class can name itself in a hint and import cycles do not bite.

### Dataclasses: values that cannot change

```python
@dataclass(frozen=True, slots=True)
class ConsentTerms:
    payer_account: str
    payee_account: str
    client_id: str
    max_amount: Money
    scope: ConsentScope
    period: timedelta | None = None

    def __post_init__(self) -> None:
        if not self.max_amount.is_positive():
            raise InvalidProofError("a consent needs a positive maximum amount")
        if (self.scope is ConsentScope.RECURRING) != (self.period is not None):
            raise InvalidProofError("a recurring consent needs a period and a single-payment consent has none")
```

A **dataclass** writes `__init__`, `__repr__` and `__eq__` for you. `frozen=True` makes instances **immutable**: the terms a customer approved cannot be edited afterwards. `slots=True` fixes the attribute list. `__post_init__` runs right after the generated `__init__` and refuses nonsense, so an invalid `ConsentTerms` cannot even be built. `timedelta | None` means "a duration, or nothing". Changes produce new values: `Consent.revoked(now)` returns `dataclasses.replace(self, revoked_at=now)`, a copy with one field changed.

### Money is an integer, never a float

`Money(5000, "USD")` is 50.00 US dollars: **5000 minor units** (cents). Its `__post_init__` refuses non-integers (checking `bool` first, because `bool` is a subclass of `int`), and its operators refuse to mix currencies. Text is read with `decimal.Decimal`, which stores decimal digits exactly.

### Enums with meaning

```python
class Refusal(StrEnum):
    CONSENT_REVOKED = "consent_revoked"
    CONSENT_EXPIRED = "consent_expired"
    AMOUNT_EXCEEDS_CONSENT = "amount_exceeds_consent"
    PERIOD_CAP_EXCEEDED = "period_cap_exceeded"
    ...
```

An **enum** is a fixed set of named values; `StrEnum` (Python 3.11 and newer) makes each also a real string, ready to travel in JSON. The lesson demands a **distinct, clear reason for every refusal**, so there is one member per reason, never a generic "denied".

### A decision as a pure function

```python
def refusal_for(self, request, caller_client_id, now, spent_in_period) -> Refusal | None:
    if self.terms.client_id != caller_client_id:
        return Refusal.NOT_GRANTED_TO_CALLER
    if request.payee_account != terms.payee_account:
        return Refusal.PAYEE_NOT_COVERED
    if self.revoked_at is not None:
        return Refusal.CONSENT_REVOKED
    if now >= self.expires_at:
        return Refusal.CONSENT_EXPIRED
    ...
    return None
```

The method gets everything it needs as arguments (including `now`), returns the **first** reason a payment may not happen, and `None` when it may. The order is fixed (whose consent, which payee, revoked, expired, already used, currency, amount, period cap), so the same input always gives the same answer. Because it has no clock or database inside, tests call it directly.

### State machines as values

```python
def attempt(self, code_matches: bool, now: datetime) -> tuple[Challenge, AttemptOutcome]:
    ...
    attempts = self.attempts + 1
    if attempts >= MAX_ATTEMPTS:
        return dataclasses.replace(self, attempts=attempts, status=ChallengeStatus.LOCKED), AttemptOutcome.LOCKED
    return dataclasses.replace(self, attempts=attempts), AttemptOutcome.WRONG_CODE
```

`Challenge.attempt` returns a **tuple**: the challenge as it must now be stored, and what happened. A wrong code counts; the third locks the challenge (`MAX_ATTEMPTS = 3`). The caller stores the returned value; nothing is changed in place.

### Protocols: the "ports"

```python
class BankGateway(Protocol):
    def initiate_payment(self, *, consent_id: str, payee_account: str, amount: Money, idempotency_key: str) -> Paid | Refused: ...
```

A `Protocol` describes a shape: "anything with this method". The merchant's checkout depends on `BankGateway`; the real class talks HTTP, and a test can pass a fake. The bank's `Crypto` and the unit-of-work ports work the same way. `Paid | Refused` is a **union**: the result is one of two small dataclasses, which callers tell apart with `isinstance`.

### Time, URLs, encodings

`datetime` with `tzinfo=UTC` is a precise instant; `timedelta` is a duration. `urllib.parse.urlsplit` and `urlunsplit` cut a URL into parts and rebuild it (used to bind a proof to a URL); `parse_qs` reads a form body; `base64.urlsafe_b64encode` and `b64decode` convert bytes to and from text; `json.dumps(..., sort_keys=True, separators=(",", ":"))` writes JSON in a canonical form.

### Files with restricted permissions

```python
path.touch(mode=0o600, exist_ok=True)
path.chmod(0o600)
```

`pathlib.Path` represents file paths as objects. `0o600` is an octal file mode: owner read and write only. Used for private keys, the PIN files and the file that stands in for the customer's phone.

### Concurrency primitives

```python
with self._lock:
    self._items[checkout.checkout_id] = checkout
```

`threading.Lock` lets one thread at a time into a block. The merchant's small in-memory store (`InMemoryCheckouts`) and the fault script use it, because the HTTP server runs requests in several threads.

### Programs with arguments and secrets

`argparse` (with `add_subparsers`) defines commands such as `open-banking-admin register-merchant --client-id merchant-1 --name Shop`. `secrets.token_urlsafe(32)` and `secrets.randbelow(10**6)` draw secure random values for client secrets and PINs. `getpass.getpass("PIN: ")` reads a PIN from the terminal without echoing it. Each program ends with `if __name__ == "__main__": sys.exit(main())`, and `main` returns an exit status (0 success, 1 refused, 2 configuration error).

### Context managers, exceptions, generics

`with self._uow_factory() as uow:` opens a database transaction and commits or rolls back on exit (see `psycopg.md`). `raise X(...) from error` keeps the original error as the cause. `def retry[T](...)` (Python 3.12 syntax) declares a type variable for the generic retry helper.

## Try it yourself

1. A consent can only be created with the matching proof:

   ```bash
   .venv/bin/python - <<'EOF'
   from datetime import UTC, datetime, timedelta
   from open_banking.domain.money import Money
   from open_banking.domain.consent import AuthenticationProof, Consent, ConsentScope, ConsentTerms
   now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
   terms = ConsentTerms("acct-ada", "acct-shop", "merchant-1", Money(5000, "USD"), ConsentScope.SINGLE_PAYMENT)
   bigger = ConsentTerms("acct-ada", "acct-shop", "merchant-1", Money(500000, "USD"), ConsentScope.SINGLE_PAYMENT)
   proof = AuthenticationProof("p1", "c1", terms.digest(), now + timedelta(minutes=2))
   print(Consent.grant("k1", proof, terms, now, timedelta(minutes=10)).expires_at.isoformat())
   try:
       Consent.grant("k2", proof, bigger, now, timedelta(minutes=10))
   except Exception as error:
       print(type(error).__name__, error)
   EOF
   ```

   Expected: `2026-10-27T12:10:00+00:00`, then `InvalidProofError the proof of authentication was issued for different terms`.

2. Ask the rules why a payment is refused:

   ```bash
   .venv/bin/python - <<'EOF'
   from datetime import UTC, datetime, timedelta
   from open_banking.domain.money import Money
   from open_banking.domain.consent import AuthenticationProof, Consent, ConsentScope, ConsentTerms, PaymentRequest
   now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
   terms = ConsentTerms("acct-ada", "acct-shop", "merchant-1", Money(5000, "USD"), ConsentScope.SINGLE_PAYMENT)
   consent = Consent.grant("k1", AuthenticationProof("p1", "c1", terms.digest(), now + timedelta(minutes=2)), terms, now, timedelta(minutes=10))
   request = PaymentRequest("k1", "acct-shop", Money(6000, "USD"))
   refusal = consent.refusal_for(request, "merchant-1", now, Money.zero("USD"))
   print(refusal.value, "|", consent.explain(refusal, request, Money.zero("USD")))
   EOF
   ```

   Expected: `amount_exceeds_consent | amount 60.00 exceeds consent max 50.00`.

3. Three wrong codes lock a challenge:

   ```bash
   .venv/bin/python - <<'EOF'
   from datetime import UTC, datetime, timedelta
   from open_banking.domain.authentication import Challenge
   now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
   challenge = Challenge("ch1", "c1", "digest", "code-digest", now, now + timedelta(minutes=5))
   for _ in range(3):
       challenge, outcome = challenge.attempt(False, now)
       print(outcome.value, challenge.status.value)
   EOF
   ```

   Expected: `wrong_code PENDING`, `wrong_code PENDING`, `locked LOCKED`.

4. Immutability: `.venv/bin/python -c "from open_banking.domain.money import Money; m = Money(1, 'USD'); m.minor = 2"` raises `FrozenInstanceError`.

## Mistakes people make

- **Using floats for money.** `0.1 + 0.2` is not `0.3`; this code stores integers.
- **Comparing secrets with `==`.** Use `hmac.compare_digest` (see `hashing-hmac-and-scrypt.md`).
- **Mutable default arguments** (`def f(x=[])`): shared by every call. Use `field(default_factory=...)`.
- **Naive datetimes.** Mixing aware and naive times raises errors; every instant here is UTC-aware.
- **Using `random` for secrets.** Use `secrets`.

## Check yourself

1. What does `frozen=True` guarantee for `ConsentTerms`? *The approved terms cannot be edited after creation.*
2. Why does `refusal_for` take `now` as a parameter? *So the decision is a pure function that tests can call with any time.*
3. What does `Challenge.attempt` return and why? *The updated challenge and the outcome, so the caller stores the new value; nothing changes in place.*
4. What does mode `0o600` mean? *Only the owner can read or write the file.*
5. What is `Paid | Refused`? *A union: the result is one of two small classes, told apart with `isinstance`.*

## Further reading

- Python 3.12 tutorial and reference: https://docs.python.org/3.12/
- `dataclasses`: https://docs.python.org/3.12/library/dataclasses.html
- `enum` (`StrEnum`): https://docs.python.org/3.12/library/enum.html
- `datetime`: https://docs.python.org/3.12/library/datetime.html
- `pathlib`: https://docs.python.org/3.12/library/pathlib.html
- `argparse`: https://docs.python.org/3.12/library/argparse.html
- `typing.Protocol`: https://docs.python.org/3.12/library/typing.html#typing.Protocol
