# Hypothesis

## What it is

Hypothesis is a Python library for **property-based testing**. Instead of writing a few example inputs by hand, you describe the *kind* of input (a list of up to 25 steps, each an operation and a number) and a *property* that must always hold (the ledger sums to zero). Hypothesis then generates many random inputs, runs your test on each, and when one fails it **shrinks** the input to the smallest one that still fails, so the bug report is readable.

## Why this repository uses it

The lesson's guarantees are claims about *every* possible history: no account is ever overdrawn, no money is created, no payment happens without a consent that covers it, no payment happens after a revocation. A person can write a handful of scenarios; a generator can try hundreds of orderings of "grant a consent", "pay", "pay again with the same key", "revoke", "let time pass", "tamper with the amount" and "pay as another merchant", and check the guarantees after every step.

## Where it appears

| Path | Role |
| --- | --- |
| `tests/unit/application/test_payments.py` | The property test `test_no_sequence_of_operations_overdraws_an_account_creates_money_or_pays_without_a_consent`. |
| `pyproject.toml` | `hypothesis>=6.100` in the `dev` extra. |
| `.gitignore` | Ignores the `.hypothesis/` folder where Hypothesis keeps its example database. |

## How it works underneath

- A **strategy** (`st.integers()`, `st.lists(...)`, `st.sampled_from(...)`) knows how to generate and shrink one kind of value.
- `@given(...)` turns a test into a loop: draw values from the strategies, call the test, repeat up to `max_examples` times (default 100).
- If a call fails, Hypothesis tries simpler variants of the failing input until it finds a minimal failing one, prints it, and saves it in `.hypothesis/` so the next run tries it first.

## Walkthrough of this repository's use

```python
OPERATIONS = st.sampled_from(["grant", "pay", "pay_again", "revoke", "tick", "tamper", "other_merchant"])

@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture]
)
@given(steps=st.lists(st.tuples(OPERATIONS, st.integers(min_value=1, max_value=70_000)), max_size=25))
def test_no_sequence_of_operations_overdraws_an_account_creates_money_or_pays_without_a_consent(steps):
    world = build_world()
    ...
```

- `st.sampled_from([...])` picks one operation; `st.tuples(a, b)` pairs it with an amount from `st.integers(1, 70_000)` (up to 700.00); `st.lists(..., max_size=25)` makes a history of up to 25 steps.
- `max_examples=40`: forty generated histories per run, to keep the unit suite quick.
- `deadline=None`: Hypothesis normally fails a test that takes more than 200 ms for one example; each example builds a whole in-memory bank, so the deadline is off.
- `suppress_health_check=[...]`: silences two warnings. `too_slow` is for slow data generation; `function_scoped_fixture` is Hypothesis's warning about combining `@given` with pytest fixtures that are set up once per test function rather than once per generated example; the test builds its own fresh world inside the body, so the warning does not apply.

The body replays the steps against a fresh in-memory world:

```python
for index, (operation, amount) in enumerate(steps):
    try:
        if operation == "grant":
            consent_ids.append(grant(world, terms(maximum=amount)).consent_id)
        elif consent_ids and operation in {"pay", "pay_again"}:
            consent_id = consent_ids[amount % len(consent_ids)]
            key = f"k-{index}" if operation == "pay" else f"k-{amount % 5}"
            pay(world, consent_id, amount, key)
        elif consent_ids and operation == "revoke": ...
        elif operation == "tick":
            world.clock.advance(timedelta(minutes=amount % 15))
        elif consent_ids and operation == "tamper":
            pay(world, consent_ids[0], amount, f"t-{index}", signed_minor=amount + 1)
        elif consent_ids and operation == "other_merchant": ...
    except (PaymentRefusedError, IdempotencyKeyReusedError):
        pass
    assert world.db.ledger_sum() == 0
    assert world.db.balance("acct-ada") >= 0
```

- Operations that need an existing consent are skipped while there is none.
- `pay` uses a new key each time; `pay_again` reuses one of five keys, so replays, key reuse with a different amount (refused) and fresh payments are all generated.
- `tick` moves the fake clock, so consents expire during the history. `tamper` sends a request whose signature covers a different amount. `other_merchant` pays as a different registered merchant.
- Refusals (`PaymentRefusedError`, `IdempotencyKeyReusedError`) are valid steps and are swallowed: the properties must hold after them too. Any other exception is a bug and fails the test.
- After **every** step: the ledger sums to zero and Ada's balance is not negative.

After the loop it checks properties of the whole history:

```python
assert world.db.balance("acct-ada") + world.db.balance("acct-shop") == 50_000
for payment in world.db.payments.values():
    consent = world.db.consents[payment.consent_id]
    assert payment.amount.minor <= consent.terms.max_amount.minor
    assert payment.created_at < consent.expires_at
    assert payment.client_id == consent.terms.client_id
    assert consent.revoked_at is None or payment.created_at <= consent.revoked_at
```

Money moved between Ada and the shop only (their total is unchanged); every payment is within its consent's cap and time, was made by the merchant the consent was granted to, and was not made after a revocation; and payment ids are unique.

## Try it yourself

1. Run the property test:

   ```bash
   .venv/bin/python -m pytest tests/unit/application/test_payments.py -q -k "no_sequence"
   ```

   Expected: `1 passed`.

2. Show the statistics:

   ```bash
   .venv/bin/python -m pytest tests/unit/application/test_payments.py -q -k "no_sequence" --hypothesis-show-statistics
   ```

   Expected: a `Hypothesis Statistics` block with a line such as `40 passing, 0 failing, and N invalid test cases` and `Stopped because settings.max_examples=40`.

3. See shrinking: in a scratch copy, add `assert len(steps) < 3` at the end of the loop body and run the test. Expected: Hypothesis reports a falsifying example with three steps or fewer (the smallest list that fails). Undo your change.

4. Look at Hypothesis's working folder: `ls .hypothesis`. Expected: at least a `constants` folder; failing examples are saved there.

## Mistakes people make

- **A property that restates the code.** Pick properties true for a different reason (here: double-entry accounting and consent rules).
- **Swallowing too many errors.** Only the named refusals are caught.
- **Leaving the 200 ms deadline on** for slow bodies, which produces flaky failures.
- **Putting I/O in the loop.** This test uses an in-memory bank so thousands of steps stay fast; the real database gets its own concurrency tests.
- **Forgetting that examples are random.** The saved database replays a failure.

## Check yourself

1. What does `@given` do? *Turns a test into a loop over generated inputs.*
2. What is shrinking? *Reducing a failing input to the smallest one that still fails.*
3. Why are `PaymentRefusedError`s ignored in the loop? *Refusals are valid steps; the guarantees must still hold after them.*
4. Which properties are checked after every step? *The ledger sums to zero and the payer's balance is not negative.*
5. What does the `tick` operation test? *That consents expire as time passes, and no payment happens after expiry.*

## Further reading

- Hypothesis documentation (6.x): https://hypothesis.readthedocs.io/en/latest/
- Quickstart: https://hypothesis.readthedocs.io/en/latest/quickstart.html
- Settings (`max_examples`, `deadline`, health checks): https://hypothesis.readthedocs.io/en/latest/reference/api.html#settings
- Strategies: https://hypothesis.readthedocs.io/en/latest/reference/strategies.html
