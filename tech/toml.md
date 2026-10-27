# TOML

## What it is

TOML (Tom's Obvious, Minimal Language) is a plain-text **configuration file format**. It is built from `key = value` lines, grouped under `[table]` headings. It is designed to be easy to read and to have one obvious meaning. Python's standard library reads it (`tomllib`), and Python tools read their settings from a file named `pyproject.toml`.

## Why this repository uses it

`pyproject.toml` is the one place that says what the project is (name, Python version, dependencies, command-line programs) and how each development tool behaves (Ruff, mypy, pytest, coverage, import-linter). One file, one format, no scattered config files.

## Where it appears

| Path | Role |
| --- | --- |
| `pyproject.toml` | Project metadata and every tool's settings (121 lines). |

## How it works underneath

A program opens the file, parses the text into nested dictionaries and lists, and reads what it needs. Tools look for their own table: Ruff reads `[tool.ruff]`, mypy `[tool.mypy]`, and so on. Packaging tools read `[build-system]` and `[project]`.

## Walkthrough of this repository's use

### The syntax in this file

```toml
[project]
name = "payments-secure-open-banking"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115,<1",
    "psycopg[binary,pool]>=3.2,<4",
    "pyjwt[crypto]>=2.9,<3",
]
```

- `[project]` starts a **table** (a group of keys). `[tool.ruff.lint]` is a table nested inside `tool` then `ruff`.
- `key = value`. Strings are in double quotes. Arrays use `[ ... ]` and may span lines; a trailing comma is allowed.
- `license = { text = "MIT" }` is an **inline table**, a table written on one line.
- `[[tool.mypy.overrides]]` (double brackets) adds one element to an **array of tables**; writing it again adds another. `[[tool.importlinter.contracts]]` appears three times for the three contracts, and `[[tool.mypy.overrides]]` twice.
- `#` starts a comment.

### Sections and what reads them

| Section | Read by | What it sets |
| --- | --- | --- |
| `[build-system]` | pip, via the build backend | `requires = ["setuptools>=75"]`, `build-backend = "setuptools.build_meta"`: how to build the package. |
| `[project]` | pip, setuptools | Name, version, description, `readme`, `requires-python`, license, author, `dependencies`. |
| `[project.optional-dependencies]` | pip | The `dev` extra (pytest, pytest-cov, hypothesis, ruff, mypy, import-linter, pip-audit). |
| `[project.scripts]` | pip | Command names to create. There are six: `open-banking-bank`, `-merchant`, `-admin`, `-customer`, `-demo` and `-health`. `open-banking-bank = "open_banking.interfaces.serve_bank:main"` means "a command that imports that module and calls `main`". |
| `[tool.setuptools.packages.find]` | setuptools | `where = ["src"]`: the packages are under `src/`. |
| `[tool.ruff]`, `[tool.ruff.lint]`, `[tool.ruff.lint.per-file-ignores]` | Ruff | Line length, rule selection, test exceptions (see `ruff.md`). |
| `[tool.mypy]`, `[[tool.mypy.overrides]]` | mypy | Strict typing, a relaxed rule for tests, and `ignore_missing_imports` for the `jwt` package (see `mypy.md`). |
| `[tool.pytest.ini_options]` | pytest | Test paths, options, markers (see `pytest.md`). |
| `[tool.coverage.run]`, `[tool.coverage.report]` | coverage.py | Source and branch settings (see `coverage-and-pytest-cov.md`). |
| `[tool.importlinter]`, contracts | import-linter | The layer rules, the library ban for domain and application, and the merchant-isolation rule (see `import-linter.md`). |

### Version ranges in `dependencies`

`"fastapi>=0.115,<1"`: any version from 0.115 up to but excluding 1.0. The comma means "and". `psycopg[binary,pool]` asks for two extras of the package. The exact versions actually installed come from the lock files.

### The `src` layout

`packages.find where = ["src"]` plus code under `src/open_banking/` means the package is only importable after installation (`pip install -e .`). Tests therefore exercise the installed package, as users would.

## Try it yourself

1. Parse the file with Python's own TOML reader and print the project name:

   ```bash
   .venv/bin/python -c "import tomllib; d = tomllib.load(open('pyproject.toml','rb')); print(d['project']['name'], d['project']['requires-python'])"
   ```

   Expected: `payments-secure-open-banking >=3.12`.

2. List the tool tables:

   ```bash
   .venv/bin/python -c "import tomllib; print(sorted(tomllib.load(open('pyproject.toml','rb'))['tool']))"
   ```

   Expected: `['coverage', 'importlinter', 'mypy', 'pytest', 'ruff', 'setuptools']`.

3. See the commands the file creates: `ls .venv/bin | grep open-banking`. Expected: `open-banking-admin`, `open-banking-bank`, `open-banking-customer`, `open-banking-demo`, `open-banking-health`, `open-banking-merchant`.

## Mistakes people make

- **Single quotes vs double quotes.** Both are strings (single quotes are literal, no escapes); this file uses double quotes.
- **Forgetting that `[[x]]` repeats.** Writing `[x]` twice is an error.
- **Misspelling a table name.** A tool then silently uses defaults. `[tool.ruff]` must be exact.
- **Duplicating a key** inside one table (an error).
- **Loose versions in production.** Ranges live here; exact pins live in the lock.

## Check yourself

1. What does `[[tool.importlinter.contracts]]` mean? *One more element in a list of contract tables.*
2. Where do the six console commands come from? *`[project.scripts]`.*
3. What does `requires-python = ">=3.12"` do? *pip refuses to install the project on older Pythons.*
4. Which Python module reads TOML? *`tomllib` (3.11 and newer).*
5. Why does the file contain settings for tools, not just the project? *Each tool reads its own `[tool.<name>]` table, so there is one config file.*

## Further reading

- TOML 1.0 specification: https://toml.io/en/v1.0.0
- `tomllib`: https://docs.python.org/3.12/library/tomllib.html
- Writing `pyproject.toml`: https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
- setuptools configuration: https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html
