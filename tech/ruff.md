# Ruff

## What it is

Ruff is a very fast tool, written in Rust, that does two jobs for Python code. As a **linter** (`ruff check`) it reads your source and reports likely mistakes and style problems, such as an unused import. As a **formatter** (`ruff format`) it rewrites the layout of the code (line breaks, quotes, spacing) into one consistent style so nobody argues about it.

## Why this repository uses it

A reader should see the same style in every file, and obvious defects (unused names, missing docstrings on public functions, risky patterns) should never reach review. `make lint` runs Ruff first, and CI runs `make check`, which includes it.

## Where it appears

| Path | Role |
| --- | --- |
| `pyproject.toml`, `[tool.ruff]` and below | The settings. |
| `Makefile` targets `lint` and `format` | Run `ruff check .` and `ruff format --check .` (or `--fix` and format). |
| `.ruff_cache/` | Ruff's cache (ignored by git). |

## How it works underneath

Ruff parses each `.py` file into a syntax tree, runs a set of **rules** over it, each with a code such as `F401` (unused import), and prints a message with the file, line and column. Some rules have an automatic fix (`--fix`). The formatter prints the tree back out in a fixed style, similar to the Black formatter. Neither runs your code.

## Walkthrough of this repository's use

```toml
[tool.ruff]
line-length = 120
target-version = "py312"
src = ["src", "tests"]
extend-exclude = ["tech"]
```

- `line-length = 120`: the formatter wraps at 120 characters and the linter flags longer lines (rule `E501`).
- `target-version = "py312"`: write and accept syntax valid for Python 3.12 (so the rules know that `def retry[T]` and `X | None` are fine, and can suggest modern forms).
- `src`: where the project's own packages live, so the import sorter can tell *first-party* imports (`open_banking`, `tests`) from third-party ones.
- `extend-exclude = ["tech"]`: skip the teaching notes. Recent Ruff versions also format Python code fences inside Markdown files, and the notes quote excerpts that are not complete programs.

```toml
[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "N", "C4", "RUF", "PT", "D100", "D101", "D103", "D104"]
```

`select` turns on rule families by code prefix:

| Prefix | Family | Catches |
| --- | --- | --- |
| `E` | pycodestyle errors | Line length, whitespace slips. |
| `F` | Pyflakes | Unused imports and variables, undefined names. |
| `I` | isort | Imports not sorted into the standard groups (standard library, third party, first party). |
| `B` | flake8-bugbear | Likely bugs: mutable default arguments, `raise` without `from` inside `except`. |
| `UP` | pyupgrade | Old syntax that has a modern form for Python 3.12. |
| `SIM` | flake8-simplify | Needlessly complicated code. |
| `N` | pep8-naming | Class, function and constant naming. |
| `C4` | flake8-comprehensions | Unnecessary `list(...)` or `dict(...)` calls. |
| `RUF` | Ruff-specific | A few extra checks. |
| `PT` | flake8-pytest-style | Consistent pytest style. |
| `D100`, `D101`, `D103`, `D104` | pydocstyle | A docstring is required on every module, public class, public function and package. |

```toml
[tool.ruff.lint.per-file-ignores]
"tests/**" = ["D", "PT018"]  # PT018: a compound assert states one fact in these tests
```

In tests, docstring rules are off (test names are sentences) and `PT018` ("break a compound assertion into several") is off because lines such as `assert first == second and not first_replayed and second_replayed` state one fact on purpose.

### What the commands do

```make
lint:
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy
	$(VENV)/bin/lint-imports
format:
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .
```

`--check` on the formatter changes nothing; it only fails if a file *would* change. `make format` applies the safe fixes and the formatting.

## Try it yourself

1. Lint the repository:

   ```bash
   .venv/bin/ruff check .
   ```

   Expected: `All checks passed!`

2. Check formatting:

   ```bash
   .venv/bin/ruff format --check .
   ```

   Expected: `84 files already formatted` (the number changes as files are added).

3. See a rule fire on a scratch file:

   ```bash
   printf 'import os\nx=1\n' > /tmp/demo.py
   .venv/bin/ruff check /tmp/demo.py --select F,I --isolated
   ```

   Expected: a message such as `I001 [*] Import block is un-sorted or un-formatted` (and `F401` for the unused `os`); `[*]` means a fix is available. Delete the file afterwards.

4. Ask what a rule means: `.venv/bin/ruff rule B006` prints the explanation of the mutable-default-argument rule.

## Mistakes people make

- **Silencing a rule with `# noqa` instead of fixing it.** This repository keeps its exceptions in one place, `per-file-ignores`, with a reason.
- **Running the formatter and the linter in the wrong order.** `make format` runs `check --fix` first, then format, so the formatter has the last word.
- **Mixing `isort` ordering by hand.** Let `I` do it.
- **Assuming Ruff checks types.** It does not; mypy does.

## Check yourself

1. What is the difference between `ruff check` and `ruff format`? *The first reports problems (and can fix some); the second rewrites layout.*
2. What does `target-version = "py312"` tell Ruff? *Which Python syntax and rules apply.*
3. Why are docstring rules off for `tests/**`? *Test names are descriptive sentences; docstrings would repeat them.*
4. What does `--check` do on `ruff format`? *Fails if any file would be reformatted, without changing files.*
5. Why is `tech` excluded? *The notes' code fences are excerpts; Ruff can format Markdown code fences and would complain.*

## Further reading

- Ruff documentation (0.17): https://docs.astral.sh/ruff/
- Rules: https://docs.astral.sh/ruff/rules/
- Configuration: https://docs.astral.sh/ruff/configuration/
- The formatter: https://docs.astral.sh/ruff/formatter/
