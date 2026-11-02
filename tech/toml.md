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

A program opens the file, parses the text into nested dictionaries and lists, and reads what it needs. Tools look for their own table: Ruff reads `[tool.ruff]`, mypy `[tool.mypy]`; each tool reads its own table. Packaging tools read `[build-system]` and `[project]`.

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

<!-- listing:pyproject.toml -->
### `pyproject.toml` (121 lines)

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "payments-secure-open-banking"
version = "0.1.0"
description = "An open-banking style payment: strong customer authentication, consent as an object, sender-constrained access, and a once-only, tamper-evident initiation."
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [{ name = "Sumit Srivastava" }]
dependencies = [
    "fastapi>=0.115,<1",
    "uvicorn>=0.32,<1",
    "psycopg[binary,pool]>=3.2,<4",
    "httpx>=0.27,<1",
    "pyjwt[crypto]>=2.9,<3",
]
```

`[build-system]` tells pip how to build the project: it needs `setuptools>=75` and uses `setuptools.build_meta`.

`[project]` is the project's identity: `name`, `version`, a `description`, the `readme` file, `requires-python = ">=3.12"`, a `license` and `authors`. `dependencies` lists the runtime packages as version ranges: `fastapi>=0.115,<1` means "0.115 or newer, below 1.0"; `psycopg[binary,pool]` asks for two extras of the package (a prebuilt binary and the connection pool); `pyjwt[crypto]` asks for PyJWT with its cryptography extra, which supplies the elliptic-curve signatures.

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8,<10",
    "pytest-cov>=5",
    "hypothesis>=6.100",
    "ruff>=0.6",
    "mypy>=1.11",
    "import-linter>=2.0",
    "pip-audit>=2.7",
]
```

`dev` is an optional group of development tools: pytest, pytest-cov, hypothesis, ruff, mypy, import-linter and pip-audit.

```toml
[project.scripts]
open-banking-bank = "open_banking.interfaces.serve_bank:main"
open-banking-merchant = "open_banking.interfaces.serve_merchant:main"
open-banking-admin = "open_banking.interfaces.admin_cli:main"
open-banking-customer = "open_banking.interfaces.customer_cli:main"
open-banking-demo = "open_banking.interfaces.demo_cli:main"
open-banking-health = "open_banking.interfaces.health_cli:main"
```

`[project.scripts]` defines six commands that installing the project creates: `open-banking-bank`, `-merchant`, `-admin`, `-customer`, `-demo` and `-health`. Each line is `command = "package.module:function"`: run that function.

```toml
[tool.setuptools.packages.find]
where = ["src"]
```

`where = ["src"]` tells setuptools the packages are under `src/`.

```toml
[tool.ruff]
line-length = 120
target-version = "py312"
src = ["src", "tests"]
# tech/ holds teaching notes whose code fences are excerpts; ruff would reformat Python fences in Markdown files.
extend-exclude = ["tech"]
```

Ruff's settings: lines are at most 120 characters, the code targets Python 3.12 and the first-party packages are in `src` and `tests`. `extend-exclude = ["tech"]` keeps Ruff off the teaching notes.

```toml
[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "N", "C4", "RUF", "PT", "D100", "D101", "D103", "D104"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["D", "PT018"]  # PT018: a compound assert states one fact in these tests
```

`select` chooses rule families by code prefix: `E` and `F` (style errors and Pyflakes mistakes), `I` (import order), `B` (likely bugs), `UP` (outdated syntax), `SIM` (simplifications), `N` (naming), `C4` (comprehensions), `RUF` (Ruff's own), `PT` (pytest style) and `D100`, `D101`, `D103`, `D104` (docstrings required on modules, public classes, public functions and packages). The per-file ignore turns off docstring rules and one pytest rule for tests, with the reason in the comment.

```toml
[tool.mypy]
python_version = "3.12"
strict = true
files = ["src", "tests"]
plugins = ["pydantic.mypy"]

# Test functions are named for the behavior they check; annotating every one adds noise, not safety.
[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false
disallow_incomplete_defs = false
disallow_untyped_calls = false

[[tool.mypy.overrides]]
module = ["jwt", "jwt.*"]
ignore_missing_imports = true
```

mypy's settings: Python 3.12, `strict = true` (full annotations and no implicit `Any`), the folders `src` and `tests`, and the Pydantic plugin. The first override relaxes the annotation requirement for tests, with the reason in the comment. The second tells mypy to accept the `jwt` package without complaint about missing type information (`ignore_missing_imports`).

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
# Starlette 1.x announces that its TestClient will move off httpx; the tests only use the client, not the transport.
filterwarnings = ["ignore:Using `httpx` with `starlette.testclient` is deprecated"]
markers = [
    "integration: needs PostgreSQL from docker-compose.yml (make up)",
    "e2e: runs the lesson's scenario through the real bank and merchant server processes against PostgreSQL (make up)",
]
```

pytest's settings: where the tests are, always-on options (`-ra` prints a summary of non-passing tests, `--strict-markers` makes unknown markers an error), a filter for a known harmless warning, and the two registered markers `integration` and `e2e`.

```toml
[tool.coverage.run]
source = ["open_banking"]
branch = true

[tool.coverage.report]
show_missing = true
skip_covered = false
```

coverage.py's settings: measure only `open_banking`, count branches (`branch = true`), show missing line numbers and do not hide fully covered files in the report.

```toml
[tool.importlinter]
root_package = "open_banking"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "Layers: interfaces -> infrastructure -> application -> domain, never upward"
type = "layers"
layers = [
    "open_banking.interfaces",
    "open_banking.infrastructure",
    "open_banking.application",
    "open_banking.domain",
]

[[tool.importlinter.contracts]]
name = "Domain and application know nothing about PostgreSQL, HTTP, the web framework or cryptography libraries"
type = "forbidden"
source_modules = ["open_banking.domain", "open_banking.application"]
forbidden_modules = ["psycopg", "psycopg_pool", "fastapi", "starlette", "uvicorn", "httpx", "pydantic", "jwt", "cryptography"]

[[tool.importlinter.contracts]]
name = "The merchant side never imports the bank's authentication, consent or payment code (it cannot see the customer's PIN or code)"
type = "forbidden"
source_modules = ["open_banking.application.merchant", "open_banking.interfaces.merchant_api", "open_banking.interfaces.serve_merchant"]
forbidden_modules = [
    "open_banking.application.bank",
    "open_banking.domain.authentication",
    "open_banking.infrastructure.otp",
    "open_banking.interfaces.bank_api",
    "open_banking.interfaces.customer_cli",
]
```

import-linter's settings. `root_package` is the package to analyze and `include_external_packages = true` also tracks third-party imports. The first contract (`type = "layers"`) lists the layers from top to bottom and forbids an import that points upward. The second (`type = "forbidden"`) says the domain and application packages may import none of `psycopg`, `psycopg_pool`, `fastapi`, `starlette`, `uvicorn`, `httpx`, `pydantic`, `jwt` or `cryptography`, not even indirectly. The third says the **merchant side** (its use cases, its API and its launcher) may import none of the bank's authentication, consent and payment code, the one-time-code channel, the bank API or the customer program, so by construction the merchant cannot see a customer's PIN or code.

<!-- /listing:pyproject.toml -->

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
