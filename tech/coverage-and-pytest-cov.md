# coverage.py and pytest-cov

## What it is

**Code coverage** measures which lines (and branches) of your program ran while the tests ran. `coverage.py` is the Python tool that records it. `pytest-cov` is a pytest plugin that starts `coverage.py` for you with the `--cov` option. A line that never ran has no test that could have caught a bug in it.

## Why this repository uses it

The domain (money, consent, authentication, the signed request) and application (the bank's and the merchant's use cases) hold all the rules about who may move whose money. The repository sets a hard bar: **95 percent or more** coverage of those two packages, enforced by `make test`, so an untested rule cannot slip in. The other packages (the database adapter, HTTP, demo) are covered by the integration and end-to-end tests instead.

## Where it appears

| Path | Role |
| --- | --- |
| `Makefile` target `test` | Runs pytest with `--cov` and then `coverage report ... --fail-under=95`. |
| `pyproject.toml` `[tool.coverage.run]` and `[tool.coverage.report]` | Settings. |
| `.gitignore` | Ignores the `.coverage` data file. |

## How it works underneath

`coverage.py` hooks Python's tracing so that, as code runs, it notes each line executed (and with branch mode on, each jump taken, such as the `if` being true and false). When the run ends it writes the facts to a SQLite file named `.coverage`. `coverage report` reads that file and prints a table.

## Walkthrough of this repository's use

```toml
[tool.coverage.run]
source = ["open_banking"]
branch = true

[tool.coverage.report]
show_missing = true
skip_covered = false
```

- `source`: measure only this package (not the tests or libraries).
- `branch = true`: also check branches. A line `if x: ...` is only fully covered if the test goes both into and past the `if`.
- `show_missing`: the report lists uncovered line numbers.
- `skip_covered = false`: list every file, even those at 100 percent.

```make
test:
	$(PY) -m pytest tests/unit --cov=open_banking --cov-report=term
	$(PY) -m coverage report --include='src/open_banking/domain/*,src/open_banking/application/*' --fail-under=95
```

1. pytest runs the unit tests and `--cov-report=term` prints the table to the terminal.
2. The second command reads the same data but includes only domain and application files, and exits non-zero if their combined coverage is under 95. That non-zero exit makes `make test` (and CI) fail.

The report columns: `Stmts` (statements), `Miss` (not run), `Branch` and `BrPart` (branches and partly-covered branches), `Cover`, `Missing` (line numbers).

### Lines excluded on purpose

```python
if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

`# pragma: no cover` tells coverage to ignore a line (or the block under an `if`). It is used for program entry lines that tests do not run and for `raise AssertionError("unreachable")` guards.

## Try it yourself

1. Run the real thing:

   ```bash
   make test
   ```

   Expected: a table where `domain/consent.py` shows `100%` and the total for all files is about `73%` (unit tests alone do not exercise the PostgreSQL adapter, the two server launchers or the demo), then the second report with the domain and application figures at or above 95%.

2. Print only the two guarded packages:

   ```bash
   .venv/bin/python -m coverage report --include='src/open_banking/domain/*,src/open_banking/application/*'
   ```

   Expected: a table of the domain and application files ending with a `TOTAL` row (99% at the time of writing, with 903 statements and 130 branches).

3. See how the bar fails a build, by asking the whole package (unit tests alone reach about 73 percent of it) to meet a bar it cannot:

   ```bash
   .venv/bin/python -m coverage report --fail-under=95; echo "exit=$?"
   ```

   Expected (right after `make test`): the whole table, then `Coverage failure: total of 73 is less than fail-under=95` and `exit=2`. `make test` avoids this by checking only domain and application.

4. A browsable report: `.venv/bin/python -m coverage html`, then open `htmlcov/index.html` (red lines are untested).

## Mistakes people make

- **Chasing 100 percent.** Coverage shows what ran, not what was asserted. A test with no assertion still "covers" a line.
- **Measuring tests themselves.** `source` limits the measurement to the package.
- **Ignoring branches.** Without `branch = true`, a missing `else` path is invisible.
- **Overusing `pragma: no cover`.** It hides untested code; it is limited here to entry points and unreachable guards.

## Check yourself

1. What does `--fail-under=95` do? *Exits non-zero if total coverage is below 95 percent.*
2. Why enforce it only on domain and application? *They hold the money rules; the adapters are covered by integration tests.*
3. What does branch coverage add? *It checks both outcomes of each decision, not just that the line ran.*
4. Where does coverage store its data? *In a SQLite file named `.coverage`.*
5. What does `# pragma: no cover` do? *Excludes that line or block from the report.*

## Further reading

- coverage.py documentation: https://coverage.readthedocs.io/
- Configuration reference: https://coverage.readthedocs.io/en/latest/config.html
- Branch coverage: https://coverage.readthedocs.io/en/latest/branch.html
- pytest-cov: https://pytest-cov.readthedocs.io/en/latest/
