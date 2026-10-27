# Pydantic

## What it is

Pydantic is a Python library that **validates data** described by type hints. You write a class whose fields have types and limits; Pydantic checks incoming data against it and either gives you a clean object or a list of exactly what is wrong. FastAPI uses it to check every request body.

## Why this repository uses it

A bank API must refuse anything unexpected at the door: an amount sent as text, an extra field, a currency in lower case, a one-time code that is not six digits, a negative maximum. Pydantic models describe each request body once, and FastAPI applies them to every request. The rules of consent and authentication stay in the domain; Pydantic checks only the **shape** of the request (an import-linter contract forbids `pydantic` in `domain` and `application`).

## Where it appears

| Path | Role |
| --- | --- |
| `src/open_banking/interfaces/bank_api.py` | `TermsBody`, `ChallengeBody`, `VerifyBody`, `ConsentBody`, `RevokeBody`, `PaymentBody`. |
| `src/open_banking/interfaces/merchant_api.py` | `CheckoutBody`, `CompleteBody`. |
| `pyproject.toml` | `plugins = ["pydantic.mypy"]`; `pydantic` is in the forbidden list for `domain` and `application`. |
| `requirements.lock` | `pydantic==2.14.1`, `pydantic-core==2.50.1` (the Rust engine that does the checking). |

## How it works underneath

When a class inherits from `BaseModel`, Pydantic reads its type hints once, at class creation, and builds a **validator**, a compiled description of the rules run by `pydantic-core` (written in Rust). Calling the model, or FastAPI doing it for you, runs the validator over the data. It returns a model instance or raises `ValidationError` with a list of errors (location, type, message). By default Pydantic is in **lax mode** and converts when sensible (`"42"` to 42); **strict mode** turns that off.

## Walkthrough of this repository's use

### The terms a customer approves

```python
class TermsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payer_account: str = Field(min_length=1, max_length=64)
    payee_account: str = Field(min_length=1, max_length=64)
    client_id: str = Field(min_length=1, max_length=64)
    max_amount_minor: int = Field(strict=True, gt=0, le=10**12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    scope: ConsentScope
    period_seconds: int | None = Field(default=None, strict=True, gt=0, le=366 * 86400)
```

- `extra="forbid"`: any field not declared is an error (`extra_forbidden`). Unknown fields are not silently ignored.
- `Field(min_length=1, max_length=64)`: length limits stop empty or enormous strings.
- `Field(strict=True, gt=0, le=10**12)`: a real integer only (text such as `"5000"` is refused), greater than zero, at most 10^12 minor units, well inside a 64-bit database column.
- `pattern=r"^[A-Z]{3}$"`: exactly three capital letters, so `usd` is refused.
- `scope: ConsentScope`: the field's type is an **enum** from the domain (`single-payment` or `recurring`). Pydantic accepts only those two values.
- `period_seconds: int | None = Field(default=None, ..., le=366 * 86400)`: optional; when present it is a positive integer of at most a year in seconds.
- A method on the model, `to_terms()`, builds the domain object `ConsentTerms` from the checked fields. The domain then applies its own rules (a recurring consent needs a period).

### Secrets stay out of printed output

```python
class ChallengeBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    pin: str = Field(min_length=1, max_length=64, repr=False)
    terms: TermsBody

class VerifyBody(BaseModel):
    code: str = Field(pattern=r"^\d{6}$", repr=False)
```

`repr=False` keeps the field out of the model's printed form, so an accidental `print(body)` or a logged exception that includes the model never shows the PIN or the code. `\d{6}` means exactly six digits.

### The payment request

```python
class PaymentBody(BaseModel):
    consent_id: str = Field(min_length=1, max_length=64)
    payee_account: str = Field(min_length=1, max_length=64)
    amount_minor: int = Field(strict=True, gt=0, le=10**12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
```

The body does **not** carry the payer account (the consent decides it) or the idempotency key and signature (those are headers). Amount and currency are checked here for shape and again by the domain (`Money`) and, for meaning, against the consent.

### From a validation error to a 400

`RequestValidationError` (FastAPI's wrapper of Pydantic's error) is caught in `http_common.install_common` and answered with status 400, code `invalid_request` and a message such as `body.terms.max_amount_minor: Input should be greater than 0`. Nothing about the failure reaches the use cases.

## Try it yourself

1. A good body and a bad one:

   ```bash
   .venv/bin/python - <<'EOF'
   from pydantic import ValidationError
   from open_banking.interfaces.bank_api import PaymentBody
   print(PaymentBody(consent_id="c1", payee_account="acct-shop", amount_minor=3000, currency="USD"))
   try:
       PaymentBody(consent_id="c1", payee_account="acct-shop", amount_minor="3000", currency="usd", admin=True)
   except ValidationError as error:
       for item in error.errors():
           print(item["loc"], item["type"])
   EOF
   ```

   Expected: `consent_id='c1' payee_account='acct-shop' amount_minor=3000 currency='USD'`, then three lines for `amount_minor` (`int_type`), `currency` (`string_pattern_mismatch`) and `admin` (`extra_forbidden`).

2. The PIN stays hidden:

   ```bash
   .venv/bin/python -c "
   from open_banking.interfaces.bank_api import VerifyBody
   print(VerifyBody(code='123456'))"
   ```

   Expected: an empty line. `print` shows only the fields not marked `repr=False`, and the model has no others. (`repr(...)` shows `VerifyBody()`.)

3. A code that is not six digits: `VerifyBody(code='12345')` raises `ValidationError` (`string_pattern_mismatch`).

## Mistakes people make

- **Relying on lax conversion for money.** `strict=True` refuses `"3000"` and `30.0`.
- **Forgetting `extra="forbid"`.** Misspelled or injected fields would be silently dropped.
- **Printing models that hold secrets.** `repr=False` is a safety net, not a license to log bodies.
- **Putting business rules in the model.** "A recurring consent needs a period" lives in the domain.
- **Using Pydantic v1 examples.** Version 2 (pinned) uses `ConfigDict` and `model_config`.

## Check yourself

1. What does `extra="forbid"` do? *Rejects fields the model does not declare.*
2. Why `repr=False` on `pin` and `code`? *So printing or logging the model never shows them.*
3. What happens to `currency="usd"`? *It fails the `^[A-Z]{3}$` pattern; the API answers 400.*
4. Which layer applies the rule "a recurring consent needs a period"? *The domain (`ConsentTerms.__post_init__`).*
5. Why is `scope: ConsentScope` an enum? *Only the two allowed values are accepted, with no extra code.*

## Further reading

- Pydantic 2.14 documentation: https://docs.pydantic.dev/2.14/
- Models and `extra`: https://docs.pydantic.dev/latest/concepts/models/#extra-data
- Fields (constraints, `repr`): https://docs.pydantic.dev/latest/concepts/fields/
- Strict mode: https://docs.pydantic.dev/latest/concepts/strict_mode/
- Validation errors: https://docs.pydantic.dev/latest/errors/validation_errors/
