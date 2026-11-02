# Python walkthrough: the domain package, line by line

## What it is

This note reads every line of the `domain` package: the named errors, money, the bank's ledger, strong customer authentication, consent, the rule for what the merchant signs, and the idempotency key. It is the line-by-line companion of `python.md`; read that note first for the language itself. Each block below is a piece of a real file, copied exactly, followed by an explanation in plain words.

A *package* is a folder of Python files that other code can import. The `domain` package is the heart of the service: the part that knows what a consent allows and why a payment is refused. It uses only Python's standard library.

## Why this repository uses it

The lesson's promises (no consent without strong authentication for exactly the approved terms, a payment only inside the consent, one clear reason for every refusal) are decided here as pure rules. Keeping them in a package that imports no web, database or cryptography code means they can be tested in microseconds, and a rule can never depend on how the data happens to be stored.

## Where it appears

| File | What it is |
| --- | --- |
| `src/open_banking/__init__.py` | The top-level package. |
| `src/open_banking/domain/__init__.py` | The domain package. |
| `src/open_banking/domain/errors.py` | The named errors. |
| `src/open_banking/domain/money.py` | Money as integer minor units. |
| `src/open_banking/domain/idempotency.py` | The idempotency key, fingerprint and stored response. |
| `src/open_banking/domain/ledger.py` | The bank's transfer and postings. |
| `src/open_banking/domain/signing.py` | The canonical request the merchant signs. |
| `src/open_banking/domain/authentication.py` | Strong customer authentication. |
| `src/open_banking/domain/consent.py` | Consent, refusals and dynamic linking. |

## How it works underneath

When Python loads a module it runs the file from top to bottom once. `import` lines load other modules; `class` and `def` lines create classes and functions; nothing inside a function body runs until the function is called. A **class** is a blueprint; an **object** (instance) is one thing built from it. A **type hint** (`x: int`, `-> str`) is a note for the programmer and for the mypy checker. Money is an integer number of *minor units* (cents). **Dynamic linking** (a term from European payment rules) means the customer's approval is bound to the exact amount and payee: here the proof of authentication carries a digest of the terms, and a consent can only be created from a proof whose digest equals the digest of the terms asked for.

## Walkthrough of this repository's use



### `src/open_banking/__init__.py` (1 lines)

```python
"""An open-banking style payment: strong authentication, consent, sender-constrained access, once-only initiation."""
```

This file is the whole top-level package. A line made only of text between three double quotes at the very top of a Python file is a **docstring**: a description that Python keeps with the module. Its presence turns the folder `open_banking/` into a **package**, a folder that other code can `import` from. The text names what the project does: an open-banking style payment, with **strong authentication** (two separate proofs of identity), **consent** (the customer's recorded permission), **sender-constrained access** (an access token that only the merchant who holds the matching key can use) and **once-only initiation** (a payment request that can be repeated safely).

### `src/open_banking/domain/__init__.py` (1 lines)

```python
"""Money, the ledger, consent, strong authentication, signing rules and idempotency keys. Standard library only."""
```

The docstring of the `domain` package. The domain is the part of the program that holds the rules and nothing else: money, the ledger, consent, strong authentication, the rule for what the merchant signs, and idempotency keys. "Standard library only" is a promise that an automatic check (import-linter, see `import-linter.md`) enforces: no web framework, no database driver and no cryptography library may be imported here.

### `src/open_banking/domain/errors.py` (107 lines)

```python
"""Named errors of the domain. Each carries a stable `code` that clients and logs can rely on."""

from __future__ import annotations
```

The module docstring says what the file is for: every error the service raises on purpose has a name, and each name carries a short stable text, its `code`, so that clients and log readers can rely on it.

`from __future__ import annotations` is a line that appears at the top of almost every module here. It tells Python to keep type hints (the `: str` and `-> None` notes) as text instead of evaluating them when the file loads. That lets a hint refer to a class that is defined later in the file and avoids circular imports.

```python
class OpenBankingError(Exception):
    """Base of every error this service raises on purpose."""

    code = "open_banking_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
```

A **class** is a blueprint for objects. `class OpenBankingError(Exception)` makes a new kind of error by inheriting from Python's built-in `Exception`: it behaves like any error that can be raised with `raise` and caught with `except`, plus whatever is added here.

`code = "open_banking_error"` is a **class attribute**: one value shared by all objects of the class. Subclasses replace it with their own text.

`def __init__(self, message: str) -> None:` is the **constructor**, the method that runs when an error object is created. `self` is the object being built. `super().__init__(message)` runs the constructor of the parent class `Exception`, so the standard behavior (printing the message in a traceback) still works. `self.message = message` also keeps the text on the object as an attribute, so the interfaces can read `error.message` without converting the error to text.

```python
class InvalidMoneyError(OpenBankingError):
    """An amount or currency that cannot be represented."""

    code = "invalid_money"
```

An amount or currency that cannot be represented (a text instead of a number, an unknown currency, too many decimals). A class body with only a docstring and a `code` is enough: it inherits the constructor.

```python
class CurrencyMismatchError(OpenBankingError):
    """Two amounts in different currencies were combined."""

    code = "currency_mismatch"
```

Raised when two amounts in different currencies are added, compared or subtracted.

```python
class UnbalancedEntryError(OpenBankingError):
    """A ledger entry that does not move one positive amount between two different accounts."""

    code = "unbalanced_entry"
```

Raised when someone tries to build a ledger transfer that does not move one positive amount between two different accounts. The domain refuses to even create such a transfer.

```python
class InvalidIdempotencyKeyError(OpenBankingError):
    """The idempotency key is missing or has characters or a length we do not accept."""

    code = "invalid_idempotency_key"
```

Raised when the `Idempotency-Key` header (the client's label for "this one intended action") is missing or has characters or a length that are not accepted.

```python
class IdempotencyKeyReusedError(OpenBankingError):
    """A stored key arrived again with a different request, so it cannot be a retry."""

    code = "idempotency_key_reused"
```

Raised when a stored key arrives again with a **different** request. The same key with the same request is a retry and gets the stored answer; the same key with another request cannot be a retry, so it is refused.

```python
class AuthenticationFailedError(OpenBankingError):
    """The customer could not be authenticated (unknown user or wrong PIN, with one answer for both)."""

    code = "authentication_failed"
```

Raised when the customer cannot be authenticated. The docstring notes that an unknown user and a wrong PIN give **one** answer, so the response never tells an attacker which user names exist.

```python
class InvalidProofError(OpenBankingError):
    """The proof of strong authentication is unknown, expired, already used, or for other terms."""

    code = "invalid_authentication_proof"
```

Raised when the proof of strong authentication is unknown, expired, already used, or issued for other terms.

```python
class ClientAuthenticationError(OpenBankingError):
    """The merchant's credential, access token or proof of possession is not valid."""

    code = "invalid_client_credential"
```

Raised when the merchant's credential, access token or proof of possession is not valid.

```python
class ConsentNotFoundError(OpenBankingError):
    """No consent has this id."""

    code = "consent_not_found"
```

No consent has this id.

```python
class PaymentNotFoundError(OpenBankingError):
    """No payment has this id for this caller."""

    code = "payment_not_found"
```

No payment has this id for this caller.

```python
class AccountNotFoundError(OpenBankingError):
    """No account has this id."""

    code = "account_not_found"
```

No account has this id.

```python
class ChallengeNotFoundError(OpenBankingError):
    """No authentication challenge has this id."""

    code = "challenge_not_found"
```

No authentication challenge has this id.

```python
class AccountNotOwnedError(OpenBankingError):
    """The payer account named in the terms does not belong to the customer who authenticated."""

    code = "account_not_owned"
```

The payer account named in the consent terms does not belong to the customer who authenticated.

```python
class UnknownClientError(OpenBankingError):
    """The merchant named in the terms is not registered with the bank."""

    code = "unknown_client"
```

The merchant named in the terms is not registered with the bank.

```python
class PaymentRefusedError(OpenBankingError):
    """The bank will not make this payment. `refusal` is the reason, in a form a program can branch on."""

    code = "payment_refused"

    def __init__(self, refusal: str, message: str) -> None:
        super().__init__(message)
        self.refusal = refusal
```

The bank will not make this payment. Unlike the other errors it has its own constructor: `def __init__(self, refusal: str, message: str)` calls the parent constructor with the message and keeps `self.refusal`, the reason as a short code a program can branch on (the codes are listed in `consent.py` as `Refusal`).

### `src/open_banking/domain/money.py` (108 lines)

```python
"""Money as an integer count of minor units (cents) with a currency. Never a float."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from open_banking.domain.errors import CurrencyMismatchError, InvalidMoneyError

# Minor-unit exponent per ISO 4217 currency this service accepts.
```

The docstring states the rule of this file: money is an **integer count of minor units** (cents for dollars) together with a currency, and never a floating-point number, because a float cannot store 0.10 exactly.

The imports: `dataclass` (explained below), `Decimal` and `InvalidOperation` from the standard library's `decimal` module (a number type that stores decimal digits exactly), and the two money errors from `errors.py`.

```python
CURRENCY_EXPONENTS: dict[str, int] = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0}
```

A **dictionary** (`dict`) maps keys to values. This one maps each supported currency code to its **exponent**: how many decimal places the currency has (2 for dollars, euros and pounds, 0 for yen). The hint `dict[str, int]` says the keys are strings and the values integers. The comment above it names the standard (ISO 4217) these codes come from.

```python
@dataclass(frozen=True, slots=True)
class Money:
    """An amount in minor units. May be negative (a credit leg); a payment amount is checked positive elsewhere."""

    minor: int
    currency: str
```

`Money` is an immutable value made of two fields: `minor` (the integer amount in minor units, so 4250 means 42.50) and `currency` (the code such as `"USD"`). The docstring notes the amount may be negative because ledger credits are negative; whether a payment amount is positive is checked elsewhere.

```python
    def __post_init__(self) -> None:
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise InvalidMoneyError(f"minor units must be an integer, got {type(self.minor).__name__}")
        if self.currency not in CURRENCY_EXPONENTS:
            raise InvalidMoneyError(f"unsupported currency {self.currency!r}; supported: {sorted(CURRENCY_EXPONENTS)}")
```

The validation that runs on every construction. `isinstance(self.minor, bool)` is checked first because `bool` is a subclass of `int` in Python (`True` equals 1) and a payment amount of `True` would otherwise pass. Then `not isinstance(self.minor, int)` rejects text, floats and anything else. An unsupported currency is rejected with a message that lists the supported ones (`sorted(CURRENCY_EXPONENTS)`). The `f"..."` text is an **f-string**: `{...}` parts are replaced by values, and `!r` would show a value with quotes. Because this check runs in the constructor, no invalid `Money` can exist anywhere in the program.

```python
    @classmethod
    def zero(cls, currency: str) -> Money:
        """Zero in `currency`."""
        return cls(0, currency)
```

`@classmethod` marks a method that belongs to the class, not to one object; its first parameter is `cls` (the class itself). `zero` is a handy constructor for an amount of zero: `cls(0, currency)`.

```python
    @classmethod
    def parse(cls, text: str, currency: str) -> Money:
        """Read a decimal string such as `42.50`; more fractional digits than the currency has is an error."""
        if currency not in CURRENCY_EXPONENTS:
            raise InvalidMoneyError(f"unsupported currency {currency!r}")
        exponent = CURRENCY_EXPONENTS[currency]
        try:
            value = Decimal(text)
        except InvalidOperation as error:
            raise InvalidMoneyError(f"not an amount: {text!r}") from error
        if not value.is_finite():
            raise InvalidMoneyError(f"not an amount: {text!r}")
        scaled = value.scaleb(exponent)
        if scaled != scaled.to_integral_value():
            raise InvalidMoneyError(f"{text!r} has more than {exponent} decimal places for {currency}")
        return cls(int(scaled), currency)
```

`parse` reads a decimal text such as `"42.50"` and returns `Money(4250, "USD")`, refusing anything that does not fit. Step by step: an unknown currency is refused. `exponent` is looked up. `Decimal(text)` converts the text exactly; if the text is not a number, `Decimal` raises `InvalidOperation`, which the `try`/`except` catches and re-raises as `InvalidMoneyError` using `raise ... from error` (that keeps the original error attached as the cause). `value.is_finite()` rejects `NaN` and infinity, which `Decimal` accepts. `value.scaleb(exponent)` moves the decimal point right by `exponent` places, so `Decimal("42.50").scaleb(2)` is `4250`. If the result still has a fractional part (`scaled != scaled.to_integral_value()`, for example "42.505" for a two-decimal currency) the text had too many decimals and is refused. Otherwise `int(scaled)` is the integer amount.

```python
    @property
    def exponent(self) -> int:
        """Decimal places of the currency."""
        return CURRENCY_EXPONENTS[self.currency]
```

`exponent` is a read-only property that returns the number of decimal places of this amount's currency, using the dictionary above.

```python
    def is_positive(self) -> bool:
        """True when the amount is greater than zero."""
        return self.minor > 0

    def is_zero(self) -> bool:
        """True when the amount is zero."""
        return self.minor == 0
```

Two small questions about an amount: is it greater than zero, and is it exactly zero. They return a `bool` (`True` or `False`).

```python
    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor <= other.minor

    def __gt__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor > other.minor

    def __ge__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor >= other.minor
```

**Operator methods.** Python calls `__add__` when you write `a + b`, `__sub__` for `a - b`, `__neg__` for `-a`, and `__lt__`, `__le__`, `__gt__`, `__ge__` for `<`, `<=`, `>`, `>=`. Defining them lets `Money` objects be used with ordinary arithmetic. Each two-operand method first calls `self._same_currency(other)`, which raises `CurrencyMismatchError` when the currencies differ, and then works on the integer `minor` values, building a new `Money` (the old ones never change).

```python
    def format(self) -> str:
        """Decimal text with the currency's own number of places, for example `42.50` or `-1.23`."""
        sign = "-" if self.minor < 0 else ""
        digits = str(abs(self.minor))
        if self.exponent == 0:
            return f"{sign}{digits}"
        digits = digits.rjust(self.exponent + 1, "0")
        return f"{sign}{digits[: -self.exponent]}.{digits[-self.exponent :]}"
```

`format` turns an amount into display text with the currency's own number of decimals: `Money(4250, "USD").format()` is `"42.50"`, `Money(-123, "USD").format()` is `"-1.23"` and yen show no decimal point. How: `sign` is `"-"` for negatives; `digits = str(abs(self.minor))` is the digits without sign; `rjust(self.exponent + 1, "0")` pads on the left with zeros so there is always at least one digit before the decimal point (5 cents becomes `"005"`); then the string is cut with **slices**: `digits[: -self.exponent]` is everything except the last `exponent` characters (the whole part) and `digits[-self.exponent :]` is the last `exponent` characters (the decimals), joined with a dot. For a currency with no decimals the digits are returned as they are.

```python
    def _same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"cannot combine {self.currency} with {other.currency}")
```

A private helper (the leading underscore is the convention for "internal"). It raises `CurrencyMismatchError` with both currency codes in the message when they differ.

```python
def sum_money(amounts: list[Money] | tuple[Money, ...], currency: str) -> Money:
    """Add `amounts`; the empty sum is zero in `currency`."""
    total = Money.zero(currency)
    for amount in amounts:
        total = total + amount
    return total
```

A plain function that adds a list or tuple of `Money` values. The type `list[Money] | tuple[Money, ...]` means "a list of Money, or a tuple of any length of Money". The empty sum is `Money.zero(currency)`; that is why the function needs the currency as an argument. The `for` loop adds each amount to a running `total` using the `+` defined above, so mixing currencies raises the mismatch error here too.

### `src/open_banking/domain/idempotency.py` (41 lines)

```python
"""The idempotency key, the fingerprint of a request, and the response stored under a key."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from open_banking.domain.errors import InvalidIdempotencyKeyError
```

The docstring names the three things in this file: the idempotency key, the **fingerprint** of a request, and the response stored under a key. The imports are standard library modules: `hashlib` (hash functions), `json`, `re` (regular expressions, a mini-language for text patterns), `Mapping` (a read-only dictionary-like type), `dataclass`, and `Any` (a type hint that means "anything").

```python
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")
```

`re.compile` prepares a regular expression once so it can be used many times. The pattern `^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$` reads: the start of the text (`^`), one letter or digit, then zero to 127 more characters from letters, digits and `_ . : -`, then the end of the text (`$`). So a key is 1 to 128 characters and cannot start with punctuation. The `r` before the quotes would mean a raw string; here backslashes are written explicitly. The leading underscore marks the name as private to the module.

```python
@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    """A client-chosen key: 1 to 128 characters from letters, digits and `_ . : -`."""

    value: str

    def __post_init__(self) -> None:
        if not _KEY.match(self.value):
            raise InvalidIdempotencyKeyError(
                "the Idempotency-Key must be 1 to 128 characters of letters, digits and _ . : -"
            )
```

`IdempotencyKey` wraps the text in an immutable value object. The client chooses the key, one per intended action, and sends it in the `Idempotency-Key` header; sending the same key again must not repeat the action. `__post_init__` checks the text against the pattern with `_KEY.match(...)` and raises `InvalidIdempotencyKeyError` with a message that states the rule. A key that fails the pattern can therefore never travel further into the program.

```python
def fingerprint(operation: str, fields: Mapping[str, Any]) -> str:
    """A SHA-256 over the operation and its fields in a canonical form: the same request always gives the same text."""
    canonical = json.dumps({"operation": operation, "fields": fields}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
```

`fingerprint` answers "is this the same request?". It builds a dictionary of the operation name and the request fields, converts it to JSON text with `json.dumps(..., sort_keys=True, separators=(",", ":"))` (sorted keys and no spaces, so the text is identical for identical data whatever order the fields were written in), turns the text into bytes with `.encode()` and computes its SHA-256 hash with `hashlib.sha256(...).hexdigest()`, a 64-character hexadecimal fingerprint. A reused key with a different amount produces a different fingerprint, which is how the service tells a retry from a mistake.

```python
@dataclass(frozen=True, slots=True)
class StoredResponse:
    """The HTTP status and JSON body a key was answered with; a repeat gets exactly this."""

    status: int
    body: Mapping[str, Any]
```

`StoredResponse` holds what a key was answered with: the HTTP `status` number and the JSON `body` (a mapping). A repeat of the same request receives exactly this. It is immutable too, so the stored answer cannot drift.

### `src/open_banking/domain/ledger.py` (35 lines)

```python
"""The bank's ledger: a transfer is one entry of two postings that sum to zero. Positive credits, negative debits."""

from __future__ import annotations

from dataclasses import dataclass

from open_banking.domain.errors import UnbalancedEntryError
from open_banking.domain.money import Money
```

The bank's ledger: a transfer is one entry of two postings that sum to zero. The docstring gives the sign convention for this project: **positive credits, negative debits** (the bank thinks of customer balances, where money received is positive). `dataclass` writes the boring parts of a class; `UnbalancedEntryError` and `Money` come from sibling files.

```python
@dataclass(frozen=True, slots=True)
class Posting:
    """One row of the ledger: an account and a signed amount (positive credit, negative debit)."""

    account_id: str
    amount: Money
```

`Posting` is one row of the ledger: an account id (text) and a signed `Money`. `frozen=True` makes it unchangeable after creation; `slots=True` stores the fields compactly.

```python
@dataclass(frozen=True, slots=True)
class Transfer:
    """Money moved from one account to another. It cannot be built unbalanced: one positive amount, two accounts."""

    debit_account: str
    credit_account: str
    amount: Money
```

`Transfer` is money moved from one account to another. The docstring says it cannot be built unbalanced: one positive amount and two accounts. It holds the debit account, the credit account and the amount.

```python
    def __post_init__(self) -> None:
        if not self.amount.is_positive():
            raise UnbalancedEntryError(f"a transfer moves a positive amount, got {self.amount.format()}")
        if self.debit_account == self.credit_account:
            raise UnbalancedEntryError("a transfer needs two different accounts")
```

`__post_init__` runs right after the object is built and refuses bad shapes with `UnbalancedEntryError`: an amount that is not positive, or the same account on both sides.

```python
    def postings(self) -> tuple[Posting, Posting]:
        """The two rows: -amount on the debited account, +amount on the credited one."""
        return Posting(self.debit_account, -self.amount), Posting(self.credit_account, self.amount)
```

The two rows of the transfer: `-self.amount` on the debited account (the `__neg__` operator of `Money` flips the sign) and `+amount` on the credited one. The return type `tuple[Posting, Posting]` says "exactly two postings".

### `src/open_banking/domain/signing.py` (25 lines)

```python
"""What the merchant signs: the exact payment request, in a form both sides compute identically."""

from __future__ import annotations

import json

from open_banking.domain.money import Money
```

What the merchant signs: the exact payment request, in a form both sides compute identically. A **signature** proves that the holder of a private key approved exactly these bytes; for the bank to check it, both sides must turn the request into the same bytes, so the function below defines that form. `json` is the standard library's JSON module.

```python
def canonical_payment_request(
    *, client_id: str, consent_id: str, payee_account: str, amount: Money, idempotency_key: str
) -> bytes:
    """The bytes the merchant signs and the bank verifies: sorted keys, no spaces, amount in minor units.

    The idempotency key is part of it, so a signature cannot be moved onto a different attempt.
    """
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

`canonical_payment_request` builds the bytes. The `*` in the parameter list means every argument must be passed by name (so a call cannot confuse the consent id with the payee). The docstring: sorted keys, no spaces, the amount in minor units; and the idempotency key is part of it, so a signature cannot be moved onto a different attempt. `document` is a dictionary of six fields: the amount in minor units, the client id, the consent id, the currency, the idempotency key and the payee account. `json.dumps(document, sort_keys=True, separators=(",", ":"))` writes the dictionary as JSON text with keys in alphabetical order and no spaces after `,` or `:` (`separators` replaces the default ones), so the text is exactly the same everywhere. `.encode()` turns the text into bytes (UTF-8), which is what a signature algorithm works on.

### `src/open_banking/domain/authentication.py` (60 lines)

```python
"""Strong customer authentication: two factors (a PIN the customer knows, a one-time code sent to a device)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
```

Strong customer authentication: two factors, a PIN the customer **knows** and a one-time code sent to a device the customer **has**. `dataclasses` is used for `dataclasses.replace` (copy an object with some fields changed); `datetime` is a point in time; `StrEnum` makes an enum whose members are texts.

```python
MAX_ATTEMPTS = 3
```

The number of wrong codes after which a challenge locks. Constants are written in capitals by convention.

```python
class ChallengeStatus(StrEnum):
    """Where an authentication challenge is."""

    PENDING = "PENDING"  # the PIN was right and a code was sent
    VERIFIED = "VERIFIED"  # the code was right; a proof was issued
    LOCKED = "LOCKED"  # too many wrong codes
```

Where an authentication challenge is: `PENDING` (the PIN was right and a code was sent), `VERIFIED` (the code was right; a proof was issued) or `LOCKED` (too many wrong codes). The text after `#` on each line is a comment.

```python
class AttemptOutcome(StrEnum):
    """What entering a code did."""

    VERIFIED = "verified"
    WRONG_CODE = "wrong_code"
    LOCKED = "locked"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"
```

What entering a code did: `verified`, `wrong_code`, `locked`, `expired` or `already_used`.

```python
@dataclass(frozen=True, slots=True)
class Challenge:
    """A challenge bound to the terms the customer is about to approve; only a digest of the code is stored."""

    challenge_id: str
    customer_id: str
    terms_digest: str
    code_digest: str
    created_at: datetime
    expires_at: datetime
    attempts: int = 0
    status: ChallengeStatus = ChallengeStatus.PENDING
```

A `Challenge` is bound to the terms the customer is about to approve, through `terms_digest`. Only a **digest** of the code (a fingerprint from which the code cannot be recovered) is stored, never the code. It also has the customer id, the creation and expiry times, the number of wrong attempts so far (default 0) and the status (default `PENDING`). The class is frozen: an attempt returns a new challenge.

```python
    def attempt(self, code_matches: bool, now: datetime) -> tuple[Challenge, AttemptOutcome]:
        """Enter a code. Returns the challenge as it must now be stored, and what happened.

        A wrong code counts; the third one locks the challenge, and a locked or expired challenge accepts nothing.
        """
        if self.status is ChallengeStatus.VERIFIED:
            return self, AttemptOutcome.ALREADY_USED
        if self.status is ChallengeStatus.LOCKED:
            return self, AttemptOutcome.LOCKED
        if now >= self.expires_at:
            return self, AttemptOutcome.EXPIRED
        if code_matches:
            return dataclasses.replace(self, status=ChallengeStatus.VERIFIED), AttemptOutcome.VERIFIED
        attempts = self.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            return dataclasses.replace(self, attempts=attempts, status=ChallengeStatus.LOCKED), AttemptOutcome.LOCKED
        return dataclasses.replace(self, attempts=attempts), AttemptOutcome.WRONG_CODE
```

Entering a code. The caller tells whether the code matched (the comparison itself is done elsewhere, in constant time), and the method returns the challenge as it must now be stored together with the outcome. The checks run in a fixed order: an already verified challenge cannot be used again (`ALREADY_USED`); a locked challenge accepts nothing (`LOCKED`); an expired challenge accepts nothing (`now >= self.expires_at` gives `EXPIRED`); a matching code gives a copy with the status `VERIFIED`. A wrong code increases the attempts; at the third (`attempts >= MAX_ATTEMPTS`) the copy is `LOCKED` and the outcome `LOCKED`; otherwise the outcome is `WRONG_CODE`. Because even the third wrong code is reported as `LOCKED`, the number of tries is limited to three.

### `src/open_banking/domain/consent.py` (182 lines)

```python
"""Consent: a scoped, expiring, revocable grant created only after strong customer authentication."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from open_banking.domain.errors import InvalidProofError
from open_banking.domain.money import Money
```

Consent: a scoped, expiring, revocable grant created only after strong customer authentication. New imports: `hashlib` (SHA-256 fingerprints), `json`, `Callable` (a function type), `timedelta` (a length of time) and `InvalidProofError`.

```python
class ConsentScope(StrEnum):
    """`single-payment` allows one payment up to the maximum; `recurring` allows payments up to a cap per period."""

    SINGLE_PAYMENT = "single-payment"
    RECURRING = "recurring"
```

The two scopes. `single-payment` allows one payment up to the maximum; `recurring` allows payments up to a cap per period.

```python
class Refusal(StrEnum):
    """Why a payment was refused. One code per reason, never a generic "denied"."""

    CONSENT_NOT_FOUND = "consent_not_found"
    NOT_GRANTED_TO_CALLER = "consent_not_granted_to_caller"
    REQUEST_NOT_AS_SIGNED = "request_not_as_signed"
    PAYEE_NOT_COVERED = "payee_not_covered"
    CONSENT_REVOKED = "consent_revoked"
    CONSENT_EXPIRED = "consent_expired"
    CONSENT_ALREADY_USED = "consent_already_used"
    CURRENCY_NOT_COVERED = "currency_not_covered"
    AMOUNT_EXCEEDS_CONSENT = "amount_exceeds_consent"
    PERIOD_CAP_EXCEEDED = "period_cap_exceeded"
    INSUFFICIENT_FUNDS = "insufficient_funds"
```

Why a payment was refused: one code per reason, never a generic "denied". In the order the checks run: the consent does not exist, it was not granted to the caller, the request is not as signed, the payee is not covered, revoked, expired, already used, the currency is not covered, the amount exceeds the consent, the period cap is exceeded, and the payer has insufficient funds.

```python
@dataclass(frozen=True, slots=True)
class ConsentTerms:
    """What the customer approves at the bank: who may pay whom, up to how much, how often.

    `period` applies to recurring consents: `max_amount` is the cap for any window of that length.
    """

    payer_account: str
    payee_account: str
    client_id: str
    max_amount: Money
    scope: ConsentScope
    period: timedelta | None = None
```

What the customer approves at the bank: the payer account, the payee account, the merchant (`client_id`), the maximum amount, the scope, and (for recurring consents) the period. The docstring says `max_amount` is the cap for any window of that length.

```python
    def __post_init__(self) -> None:
        if not self.max_amount.is_positive():
            raise InvalidProofError("a consent needs a positive maximum amount")
        if (self.scope is ConsentScope.RECURRING) != (self.period is not None):
            raise InvalidProofError("a recurring consent needs a period and a single-payment consent has none")
```

Two rules checked when terms are built: the maximum must be positive, and `(self.scope is ConsentScope.RECURRING) != (self.period is not None)` must be false, which means a recurring consent needs a period and a single-payment consent has none (`!=` between two true/false values is true when they differ). Otherwise `InvalidProofError` is raised.

```python
    def digest(self) -> str:
        """A SHA-256 of the terms. A proof of authentication carries it, so the customer's approval is tied to
        exactly these terms (dynamic linking): a proof for 50.00 cannot create a consent for 5,000.00."""
        document = {
            "client_id": self.client_id,
            "currency": self.max_amount.currency,
            "max_amount_minor": self.max_amount.minor,
            "payee_account": self.payee_account,
            "payer_account": self.payer_account,
            "period_seconds": None if self.period is None else int(self.period.total_seconds()),
            "scope": self.scope.value,
        }
        return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
```

A SHA-256 of the terms. The docstring explains **dynamic linking**: a proof of authentication carries this digest, so the customer's approval is tied to exactly these terms, and a proof for 50.00 cannot create a consent for 5,000.00. `document` lists the fields in a fixed dictionary (the period as a whole number of seconds, or `None`), `json.dumps(..., sort_keys=True, separators=(",", ":"))` writes it in one canonical form, `.encode()` makes bytes, and `hashlib.sha256(...).hexdigest()` returns the fingerprint as hexadecimal text.

```python
@dataclass(frozen=True, slots=True)
class AuthenticationProof:
    """Evidence that a customer passed strong authentication for exactly these terms, once, recently."""

    proof_id: str
    customer_id: str
    terms_digest: str
    expires_at: datetime
    consumed: bool = False
```

Evidence that a customer passed strong authentication for exactly these terms, once, recently: a proof id, the customer, the digest of the terms, an expiry and a flag `consumed` (default `False`).

```python
@dataclass(frozen=True, slots=True)
class PaymentRequest:
    """What the merchant asks for under a consent."""

    consent_id: str
    payee_account: str
    amount: Money
```

What the merchant asks for under a consent: the consent id, the payee account and the amount.

```python
@dataclass(frozen=True, slots=True)
class Consent:
    """A consent as stored. Immutable; revocation and use return new values."""

    consent_id: str
    customer_id: str
    terms: ConsentTerms
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    used_at: datetime | None = None
```

A consent as stored. The docstring says it is immutable and that revocation and use return new values. Fields: the id, the customer, the terms, creation and expiry times, and two optional times, `revoked_at` and `used_at`, which are `None` until it happens.

```python
    @classmethod
    def grant(
        cls,
        consent_id: str,
        proof: AuthenticationProof,
        terms: ConsentTerms,
        now: datetime,
        time_to_live: timedelta,
    ) -> Consent:
        """Create a consent. Impossible without a valid proof that was issued for exactly these terms."""
        if proof.consumed:
            raise InvalidProofError("the proof of authentication was already used")
        if now >= proof.expires_at:
            raise InvalidProofError("the proof of authentication has expired")
        if proof.terms_digest != terms.digest():
            raise InvalidProofError("the proof of authentication was issued for different terms")
        return cls(consent_id, proof.customer_id, terms, now, now + time_to_live)
```

A class method (`@classmethod` above; `cls` is the class itself) that creates a consent. The docstring: impossible without a valid proof that was issued for exactly these terms. Three refusals, each raising `InvalidProofError`: the proof was already used; the proof has expired; the proof's digest differs from the terms' digest. Otherwise `cls(...)` builds the consent with the customer from the proof and an expiry of `now + time_to_live`.

```python
    def revoked(self, now: datetime) -> Consent:
        """The consent after the customer withdrew it. Revoking twice keeps the first time."""
        return self if self.revoked_at is not None else dataclasses.replace(self, revoked_at=now)
```

The consent after the customer withdrew it. If it is already revoked it is returned unchanged (`self if ... else ...`), so revoking twice keeps the first time.

```python
    def marked_used(self, now: datetime) -> Consent:
        """The consent after a payment was made under it."""
        return dataclasses.replace(self, used_at=now)
```

A copy with `used_at` set, after a payment was made under it.

```python
    def refusal_for(
        self, request: PaymentRequest, caller_client_id: str, now: datetime, spent_in_period: Money
    ) -> Refusal | None:
        """The first reason this payment may not happen under this consent, or None when it may.

        The order is fixed: whose consent, which payee, revoked, expired, already used, currency, amount, period cap.
        """
        terms = self.terms
        if self.terms.client_id != caller_client_id:
            return Refusal.NOT_GRANTED_TO_CALLER
        if request.payee_account != terms.payee_account:
            return Refusal.PAYEE_NOT_COVERED
        if self.revoked_at is not None:
            return Refusal.CONSENT_REVOKED
        if now >= self.expires_at:
            return Refusal.CONSENT_EXPIRED
        if terms.scope is ConsentScope.SINGLE_PAYMENT and self.used_at is not None:
            return Refusal.CONSENT_ALREADY_USED
        if request.amount.currency != terms.max_amount.currency:
            return Refusal.CURRENCY_NOT_COVERED
        if request.amount > terms.max_amount:
            return Refusal.AMOUNT_EXCEEDS_CONSENT
        if terms.scope is ConsentScope.RECURRING and spent_in_period + request.amount > terms.max_amount:
            return Refusal.PERIOD_CAP_EXCEEDED
        return None
```

The central rule. It returns the first reason a payment may not happen, or `None` when it may. The docstring gives the fixed order, and the code follows it: (1) the caller must be the merchant the consent was granted to (`NOT_GRANTED_TO_CALLER`); (2) the payee must be the one in the terms; (3) not revoked; (4) not expired (`now >= self.expires_at`); (5) a single-payment consent not used before; (6) the currency matches; (7) the amount does not exceed the maximum (`>` compares `Money`); (8) for a recurring consent, what was already spent in the period plus this amount must not exceed the cap. The fixed order means the same request always gets the same reason.

```python
    def explain(self, refusal: Refusal, request: PaymentRequest, spent_in_period: Money) -> str:
        """A clear sentence for a refusal, with the numbers that matter."""
        terms = self.terms
        messages: dict[Refusal, Callable[[], str]] = {
            Refusal.CONSENT_NOT_FOUND: lambda: "consent not found",
            Refusal.NOT_GRANTED_TO_CALLER: lambda: "consent was not granted to this merchant",
            Refusal.REQUEST_NOT_AS_SIGNED: lambda: (
                "amount does not match approved consent: the request signature does not cover this request"
            ),
            Refusal.PAYEE_NOT_COVERED: lambda: "consent does not cover this payee",
            Refusal.CONSENT_REVOKED: lambda: "consent revoked",
            Refusal.CONSENT_EXPIRED: lambda: "consent expired",
            Refusal.CONSENT_ALREADY_USED: lambda: "single-payment consent already used",
            Refusal.CURRENCY_NOT_COVERED: lambda: f"consent is in {terms.max_amount.currency}",
            Refusal.AMOUNT_EXCEEDS_CONSENT: lambda: (
                f"amount {request.amount.format()} exceeds consent max {terms.max_amount.format()}"
            ),
            Refusal.PERIOD_CAP_EXCEEDED: lambda: (
                f"amount {request.amount.format()} would take this period's total to "
                f"{(spent_in_period + request.amount).format()}, over the consent cap {terms.max_amount.format()}"
            ),
            Refusal.INSUFFICIENT_FUNDS: lambda: "the payer has insufficient funds",
        }
        return messages[refusal]()
```

A clear sentence for a refusal, with the numbers that matter. `messages` is a dictionary from each refusal to a small unnamed function (`lambda: ...`) that builds the text only when asked, so the f-strings that use `terms` and the request are computed only for the refusal that happened. `messages[refusal]()` looks up the function and calls it. For example `amount 60.00 exceeds consent max 50.00`.

## Try it yourself

1. Build terms, take their digest and see that a different amount changes it:

   ```bash
   .venv/bin/python -c "
   from open_banking.domain.consent import ConsentScope, ConsentTerms
   from open_banking.domain.money import Money
   a = ConsentTerms('acc-1', 'acc-2', 'merchant-1', Money(5000, 'USD'), ConsentScope.SINGLE_PAYMENT)
   b = ConsentTerms('acc-1', 'acc-2', 'merchant-1', Money(500000, 'USD'), ConsentScope.SINGLE_PAYMENT)
   print(a.digest()[:16], b.digest()[:16], a.digest() == b.digest())"
   ```

   Expected: two different 16-character prefixes and `False`.

2. Try an invalid amount: `.venv/bin/python -c "from open_banking.domain.money import Money; Money.parse('42.505','USD')"` raises `InvalidMoneyError: '42.505' has more than 2 decimal places for USD`.

3. Look at the canonical request: `.venv/bin/python -c "from open_banking.domain.signing import canonical_payment_request as c; from open_banking.domain.money import Money; print(c(client_id='m', consent_id='c', payee_account='p', amount=Money(100,'USD'), idempotency_key='k'))"` prints the JSON bytes with sorted keys and no spaces.

## Mistakes people make

- **Comparing a code with `==`.** The code comparison is done in constant time elsewhere (see `python-infrastructure.md`) to avoid leaking information by timing.
- **Letting a proof be reused.** `grant` refuses a consumed or expired proof or one for other terms.
- **Telling an attacker which user names exist.** An unknown user and a wrong PIN give the same error.
- **Checking the refusal reasons in a different order each time.** The order is fixed so the same request always gets the same reason.
- **Signing a loose description.** The merchant signs a canonical form with sorted keys and the idempotency key in it.

## Check yourself

1. What does the digest of the terms protect against? *A proof of authentication for small terms being used to create a consent for larger ones.*
2. When does a challenge lock? *At the third wrong code.*
3. Why does `canonical_payment_request` sort the keys and remove spaces? *So the merchant and the bank compute exactly the same bytes.*
4. What does `Consent.revoked` do when called twice? *It keeps the first revocation time.*
5. Which refusal comes first in `refusal_for`? *`NOT_GRANTED_TO_CALLER`, because the caller must own the consent.*

## Further reading

- Python 3.12 `dataclasses`: https://docs.python.org/3.12/library/dataclasses.html
- Python 3.12 `hashlib`: https://docs.python.org/3.12/library/hashlib.html
- Python 3.12 `json` (`sort_keys`, `separators`): https://docs.python.org/3.12/library/json.html
- Python 3.12 `enum`: https://docs.python.org/3.12/library/enum.html
