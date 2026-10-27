# pytest

## What it is

pytest is the Python **test runner**. You write functions whose names start with `test_`; pytest finds them, runs each one, and reports which passed or failed. A test **fails** when it raises an exception, usually from a plain `assert` statement that is false. pytest also provides **fixtures** (reusable setup code), **markers** (labels on tests), and **parametrization** (one test body run with many inputs).

## Why this repository uses it

The lesson's promises are claims about behavior: a consent exists only after strong authentication, a payment without a valid merchant credential is refused, a replayed initiation moves money once, a tampered amount is rejected, an abandoned or revoked consent moves no money, a timeout reconciles to one payment or none. Each becomes a test in three layers: **unit** (the bank wired to in-memory storage, with real cryptography), **integration** (the real PostgreSQL, including concurrent races) and **end-to-end** (real bank and merchant server processes and the real programs). `make test`, `make test-integration` and `make test-e2e` run them, and CI repeats them.

## Where it appears

| Path | Role |
| --- | --- |
| `tests/unit/` (198 tests) | `domain/` (consent, authentication, money, ledger, signing), `application/` (authentication and consent, access, payments, merchant checkout), `infrastructure/` (crypto and keys, logging and retry, settings and the outbox and faults), `interfaces/` (the two HTTP apps and the programs). |
| `tests/integration/` | The schema's rules, and the flows against PostgreSQL: twenty concurrent requests with one key, a race against a period cap, opposite-direction payments that must not deadlock, a revocation racing a payment, a failure while posting. |
| `tests/e2e/` | Real server processes, the real programs and the demo. |
| `tests/unit/conftest.py`, `tests/unit/fakes.py`, `tests/unit/helpers.py` | The in-memory "world" and its helpers. |
| `tests/integration/conftest.py`, `tests/e2e/conftest.py` | Fixtures for the database and for server processes. |
| `pyproject.toml`, `[tool.pytest.ini_options]` | Settings. |

In total 240 tests are collected (`pytest tests --collect-only -q`).

## How it works underneath

1. pytest **collects**: it walks `testpaths`, imports each `test_*.py` file and finds functions named `test_*`.
2. For each test it looks at the parameter names. A parameter named like a **fixture** is filled by calling that fixture function. Fixtures can depend on other fixtures.
3. It runs the test. Python's `assert` is rewritten by pytest so that a failure shows the values on both sides.
4. It reports `.` pass, `F` fail, `E` error in setup, `s` skip. Exit status 0 means all passed.
5. A fixture's **scope** says how often it is rebuilt: per test (default), per module or per session.

## Walkthrough of this repository's use

### Configuration

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
filterwarnings = ["ignore:Using `httpx` with `starlette.testclient` is deprecated"]
markers = [
    "integration: needs PostgreSQL from docker-compose.yml (make up)",
    "e2e: runs the lesson's scenario through the real bank and merchant server processes against PostgreSQL (make up)",
]
```

- `addopts`: options always added. `-ra` prints a short summary of all non-passing tests; `--strict-markers` makes an unregistered marker an error, so a typo cannot silently skip selection.
- `filterwarnings`: hides one known deprecation warning from Starlette's test client.
- `markers`: registers the two custom labels.

(`[tool.pytest.ini_options]` is the long-standing way to put pytest settings in `pyproject.toml`; pytest 9 also reads a native `[tool.pytest]` table.)

### Selecting layers

```make
test: ...
	$(PY) -m pytest tests/unit --cov=open_banking --cov-report=term
test-integration: ...
	$(PY) -m pytest tests/integration -m integration
test-e2e: ...
	$(PY) -m pytest tests/e2e -m e2e
```

`-m integration` runs only tests marked `integration`; the files set `pytestmark = pytest.mark.integration` (or `e2e`).

### The "world" fixture: a whole bank in memory

```python
@dataclass
class World:
    bank: BankContainer
    db: InMemoryDatabase
    clock: FixedClock
    phone: MemoryOtpChannel
    crypto: StandardCrypto
    merchant: Merchant
    other_merchant: Merchant
    ada_pin: str = "246810"
    bob_pin: str = "135791"

def build_world() -> World: ...

@pytest.fixture
def world() -> World:
    return build_world()
```

`build_world()` wires the bank's **real** use cases and **real cryptography** to an in-memory database, a fixed clock and an in-memory outbox (`MemoryOtpChannel`) so a test can read the one-time code. Two merchants, each with a real key pair, and two customers exist. Because the clock is fixed and only moves when a test says so, expiry and skew checks are exact. Tests then read like the lesson:

```python
consent = grant(world, terms(maximum=5000))
response, replayed = pay(world, consent.consent_id, 3000, "k-1")
assert response.status == 201 and not replayed
```

### Expecting an exception

`pytest.raises(PaymentRefusedError)` passes only if that exception is raised; the integration tests use `pytest.raises(errors.CheckViolation)` for database refusals. Tests also assert **which** reason: each refusal has its own code.

### Parametrization

`@pytest.mark.parametrize(...)` runs one body with several inputs, reported as separate tests. The domain tests use it for bad idempotency keys (empty, with a space, too long, non-ASCII) and for the shapes of terms that must be refused.

### Concurrency tests (integration)

```python
barrier = threading.Barrier(20)

def call(index: int):
    barrier.wait(timeout=30)
    return attempt(flow, consent.consent_id, 10_000, f"k-{index}")

with ThreadPoolExecutor(max_workers=20) as workers:
    outcomes = [future.result(timeout=120) for future in [workers.submit(call, i) for i in range(20)]]
assert sorted(map(str, outcomes)) == ["201"] * 5 + ["insufficient_funds"] * 15
```

Twenty threads start at the same instant (`threading.Barrier(20)` makes each wait until all twenty are ready), each trying to pay 100.00 from an account that holds 500.00, and the assertion says exactly five succeed and fifteen are refused for funds, with the ledger still summing to zero. This is the test that proves the row locks work.

### End-to-end: real processes

`tests/e2e/conftest.py` starts the bank and the merchant with `subprocess.Popen` on free ports, polls their `/health`, runs scenarios (including the `demo` program through `run_program`), and in cleanup sends `SIGTERM` and asserts exit status 0 and that no secret reached the logs.

## Try it yourself

1. Run the fast tests:

   ```bash
   make test
   ```

   Expected: `198 passed` and a coverage table, then no error from the 95 percent check.

2. List what exists without running it:

   ```bash
   .venv/bin/python -m pytest tests --collect-only -q | tail -3
   ```

   Expected: ends with `240 tests collected`.

3. Run one test by name:

   ```bash
   .venv/bin/python -m pytest tests/unit -q -k "replay"
   ```

   Expected: `3 passed, 195 deselected`.

4. See the markers at work: `.venv/bin/python -m pytest tests/unit -q -m integration` runs nothing (`198 deselected`).

5. With `make up`: `make test-integration`, then `make test-e2e`.

## Mistakes people make

- **Tests that depend on each other's leftovers.** The integration schema is reloaded before each test and the unit world is rebuilt per test.
- **Asserting too little.** A test that only checks "no exception" proves nothing; these assert statuses, refusal codes, balances and sums.
- **Real clocks and random codes in tests.** The world uses a fixed clock and a known one-time code.
- **Racy concurrency tests.** Without a barrier the threads may run one after another and prove nothing.
- **Unregistered markers.** `--strict-markers` turns the typo into an error.

## Check yourself

1. What makes a function a test? *Its name starts with `test_` in a `test_*.py` file.*
2. Why does the `world` use a fixed clock? *So expiry, skew and time-window rules are exact and repeatable.*
3. What does `threading.Barrier(20)` do in the race test? *Makes all twenty threads wait until everyone is ready, then releases them together.*
4. How do you run only the integration tests? *`pytest -m integration`, as `make test-integration` does.*
5. How does a test read the one-time code? *From the in-memory outbox, `MemoryOtpChannel`.*

## Further reading

- pytest 9.1 documentation: https://docs.pytest.org/en/9.1.x/
- Fixtures: https://docs.pytest.org/en/stable/how-to/fixtures.html
- Marks: https://docs.pytest.org/en/stable/how-to/mark.html
- Parametrize: https://docs.pytest.org/en/stable/how-to/parametrize.html
- Configuration reference: https://docs.pytest.org/en/stable/reference/customize.html
