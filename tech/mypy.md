# mypy

## What it is

mypy is a **static type checker** for Python. Python does not check type hints when it runs, so `Money("ten", 5)` would only fail at run time. mypy reads the hints (`minor: int`, `-> Money`) and checks, without running the program, that the values passed around fit those types. It reports mismatches as errors with file and line.

## Why this repository uses it

Money code is where a quiet type mix-up costs real money: an amount that is a `str`, a currency that is `None`, a function that sometimes returns nothing. Strict typing across `src/` and `tests/` makes whole categories of mistakes impossible to commit. It also verifies that the test fakes fit the same `Protocol` ports as the real adapters.

## Where it appears

| Path | Role |
| --- | --- |
| `pyproject.toml`, `[tool.mypy]` | The settings: strict mode, the Pydantic plugin, relaxed rules for tests. |
| `Makefile` target `lint` | Runs `mypy` with no arguments (the files come from the settings). |
| `.mypy_cache/` | Cache (ignored by git). |
| `src/**/*.py` | All code carries full type hints. |

## How it works underneath

mypy builds a model of every function's argument and return types, then walks every function body and checks each expression against that model. It follows imports (including the libraries' own hints) and caches results in `.mypy_cache/` so a second run only re-checks what changed. A `Protocol` class is satisfied by any class with the right methods ("structural typing").

## Walkthrough of this repository's use

```toml
[tool.mypy]
python_version = "3.12"
strict = true
files = ["src", "tests"]
plugins = ["pydantic.mypy"]
```

- `python_version`: the language version to assume.
- `strict = true`: turns on the strictest group of checks, including: every function must be annotated (`disallow_untyped_defs`), no implicit `Any`, `Optional` handling is strict (`None` must be checked), unused `type: ignore` comments are reported, and returning `Any` from a typed function is an error.
- `files`: what to check when you run bare `mypy`.
- `plugins = ["pydantic.mypy"]`: teaches mypy how Pydantic models build their `__init__` from the field definitions.

```toml
[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false
disallow_incomplete_defs = false
disallow_untyped_calls = false
```

A comment in the file explains: test functions are named for the behavior they check, and annotating each parameter adds noise, not safety. The tests still get checked for real type errors; they just do not need annotations.

### An override for an untyped library

```toml
[[tool.mypy.overrides]]
module = ["jwt", "jwt.*"]
ignore_missing_imports = true
```

The `jwt` package (PyJWT) is imported by the cryptography code. This override tells mypy not to complain that it cannot find type information for it; values coming out of `jwt` are treated as `Any` and the code narrows them with `assert isinstance(...)` and explicit annotations (for example `claims: dict[str, Any] = jwt.decode(...)`).

### Examples from the code

```python
@dataclass(frozen=True, slots=True)
class Consent:
    ...
    revoked_at: datetime | None = None
    used_at: datetime | None = None
```

`datetime | None` means "a time, or nothing". Anywhere the code uses `self.revoked_at` as a time, mypy insists on a check first, and indeed `refusal_for` writes `if self.revoked_at is not None:` before treating the consent as revoked.

```python
def retry[T](operation: Callable[[], T], ...) -> T:
```

The generic function promises to return the same `T` that `operation` returns; mypy verifies callers.

```python
key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(dict(public_jwk)))
assert isinstance(key, ec.EllipticCurvePublicKey)
```

Because `jwt` is untyped to mypy, `from_jwk` returns something broad. The `assert isinstance(...)` narrows it to an elliptic-curve **public** key for the lines below, and is also a run-time guard that refuses a key of another kind.

```python
claims: dict[str, Any] = jwt.decode(proof, key, algorithms=["ES256"], options={...})
```

An explicit annotation gives the untyped result a known shape, so later `claims.get("htm")` is checked.

```python
pool: ConnectionPool[psycopg.Connection[Any]] = ConnectionPool(...)
```

Libraries' generic types need their parameters spelled out in strict mode.

### Ports checked structurally

`Clock`, `IdSource` (in `application/ports.py`), the bank's `Crypto`, `OtpChannel` and unit-of-work ports (in `application/bank/ports.py`) and the merchant's `BankGateway` are `Protocol` classes. `StandardCrypto`, the file and memory outbox classes, `HttpBankGateway`, the in-memory fakes in `tests/unit/fakes.py` and the PostgreSQL classes are passed where a protocol is expected; mypy fails the build if a method name or type drifts.

## Try it yourself

1. Run the checker:

   ```bash
   .venv/bin/mypy
   ```

   Expected: `Success: no issues found in 76 source files` (the count changes as files are added).

2. Break a type on purpose in a scratch file (run from the repository root):

   ```bash
   printf 'from open_banking.domain.money import Money\n\nMoney("ten", "USD")\n' > scratch_bad.py
   MYPYPATH=src .venv/bin/mypy scratch_bad.py
   rm scratch_bad.py
   ```

   Expected: `scratch_bad.py:3: error: Argument 1 to "Money" has incompatible type "str"; expected "int"  [arg-type]`. (`MYPYPATH=src` makes mypy read the package from source; without it, mypy treats the editable install as an untyped library, because the package ships no `py.typed` marker, and reports `import-untyped` instead.)

3. Check mypy's version: `.venv/bin/mypy --version` (this machine: `mypy 2.4.0`).

4. See what it thinks of an expression: add `reveal_type(x)` in a scratch file and run mypy; it prints the inferred type.

## Mistakes people make

- **Treating hints as run-time checks.** They are not; `Money("ten", "USD")` is stopped at run time by `__post_init__`, and at check time by mypy.
- **Scattering `# type: ignore`.** In strict mode unused ignores are errors, which discourages leftovers. This repository contains none (`grep -rn "type: ignore" src tests` finds nothing).
- **Using `Any` to make errors go away.** It switches checking off for that value.
- **Forgetting `None`.** A function that can return nothing must say `-> X | None`.

## Check yourself

1. Does mypy run your program? *No; it only reads the code.*
2. What does `strict = true` require of every function? *Full annotations on arguments and the return.*
3. Why the `pydantic.mypy` plugin? *So mypy understands the constructors Pydantic generates.*
4. What does `Money | None` force the caller to do? *Check for `None` before using the value.*
5. How does mypy decide a fake satisfies a `Protocol`? *By having the same methods with compatible types (structural typing).*

## Further reading

- mypy documentation (2.x): https://mypy.readthedocs.io/en/stable/
- Strict mode: https://mypy.readthedocs.io/en/stable/command_line.html#cmdoption-mypy-strict
- Protocols: https://mypy.readthedocs.io/en/stable/protocols.html
- Pydantic's mypy plugin: https://docs.pydantic.dev/latest/integrations/mypy/
