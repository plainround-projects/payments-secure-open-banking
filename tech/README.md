# Technology notes

These notes teach every technology used in this repository, from zero, through the repository's own files. Each note says what the technology is, why it is here, where it appears, how it works underneath, walks through this repository's own lines, gives commands to try with the output to expect, lists common mistakes, and ends with five questions with answers. You need no earlier knowledge.

Run the commands from the repository root. Commands marked as needing the database expect `make up` first.

## If you have ten minutes

1. [python.md](python.md): the language, and how consent, authentication and money are written as immutable values and pure rules.
2. [jwt-and-dpop.md](jwt-and-dpop.md): how a merchant proves it holds its key on every request, and why a stolen token is useless.
3. [postgresql.md](postgresql.md): how the database itself keeps the ledger balanced, refuses an overdraft and makes concurrent payments take turns.

## Reading order

### 1. The language and how the project is packaged

| Note | Role here |
| --- | --- |
| [python.md](python.md) | The language of all code: dataclasses, enums, protocols, generics, context managers. |
| [pip-venv-and-lock-files.md](pip-venv-and-lock-files.md) | Installing exact, hash-checked dependencies into a private environment. |
| [uv.md](uv.md) | The tool that regenerates the lock files (`make lock` only). |
| [toml.md](toml.md) | The `pyproject.toml` format: project data and every tool's settings. |
| [environment-variables-and-dotenv.md](environment-variables-and-dotenv.md) | How configuration and the database password reach the program. |

### 2. The web layer

| Note | Role here |
| --- | --- |
| [http.md](http.md) | Requests, status codes and the security headers of both services. |
| [json.md](json.md) | The body format, canonical JSON for signatures and digests, JWKs, `jsonb`. |
| [fastapi.md](fastapi.md) | The framework behind both the bank and the merchant. |
| [pydantic.md](pydantic.md) | Strict validation of request bodies, with secrets hidden from printouts. |
| [jwt-and-dpop.md](jwt-and-dpop.md) | JWTs with PyJWT, and DPoP proof of possession. |
| [cryptography.md](cryptography.md) | Elliptic-curve keys, ES256 signatures, thumbprints. |
| [hashing-hmac-and-scrypt.md](hashing-hmac-and-scrypt.md) | scrypt for PINs and secrets, HMAC with a pepper, secure random values. |
| [uvicorn.md](uvicorn.md) | The server processes, and the graceful shutdown. |
| [httpx.md](httpx.md) | The merchant's client: authentication, retries with the same key, fault injection. |
| [logging-and-logfmt.md](logging-and-logfmt.md) | Structured logs that never hold a PIN, code, token or secret. |

### 3. The database

| Note | Role here |
| --- | --- |
| [postgresql.md](postgresql.md) | The server: constraints, deferred triggers, lock order, replay protection. |
| [sql.md](sql.md) | The language of the `sql/` folder: schema, seed, queries, migrations. |
| [psql.md](psql.md) | The command-line client that loads the SQL files. |
| [psycopg.md](psycopg.md) | The Python driver, connection pool and pipeline mode. |

### 4. Running it

| Note | Role here |
| --- | --- |
| [make.md](make.md) | The `Makefile`: one short name per task. |
| [shell.md](shell.md) | The shell syntax inside Makefile recipes, health checks and the secrets file. |
| [openssl.md](openssl.md) | `openssl rand`, which generates the pepper. |
| [docker.md](docker.md) | Containers and images in general. |
| [dockerfile.md](dockerfile.md) | The recipe that builds the image with the bank and the merchant. |
| [docker-compose.md](docker-compose.md) | The file that starts PostgreSQL (and optionally the bank). |
| [yaml.md](yaml.md) | The format of the Compose file and the CI workflow. |

### 5. Proving it works

| Note | Role here |
| --- | --- |
| [pytest.md](pytest.md) | The test runner and the three test layers, including the concurrency races. |
| [hypothesis.md](hypothesis.md) | The property test "no sequence of operations overdraws, creates money, or pays without a consent". |
| [coverage-and-pytest-cov.md](coverage-and-pytest-cov.md) | The 95 percent bar on domain and application. |
| [ruff.md](ruff.md) | Linting and formatting. |
| [mypy.md](mypy.md) | Static type checking. |
| [import-linter.md](import-linter.md) | Enforcing the layers, the library ban and the rule that the merchant cannot see the bank's side. |
| [pip-audit.md](pip-audit.md) | Checking locked dependencies for known vulnerabilities. |
| [github-actions.md](github-actions.md) | The CI workflow that repeats all of the above. |

### 6. Housekeeping

| Note | Role here |
| --- | --- |
| [git-and-ignore-files.md](git-and-ignore-files.md) | History, `.gitignore`, `.dockerignore`. |
| [editorconfig.md](editorconfig.md) | Editor settings (tabs for `Makefile`, spaces elsewhere). |
| [markdown.md](markdown.md) | The format of the README, architecture notes and decision records. |

## A note on versions

The notes describe the versions this repository pins: Python 3.12 or newer (the image uses 3.12.8; development here used 3.13), PostgreSQL 18, FastAPI 0.162, Starlette 1.10, Pydantic 2.14, psycopg 3.3, psycopg_pool 3.3, httpx 0.28, uvicorn 0.54, PyJWT 2.15, cryptography 50.0, pytest 9.1, Hypothesis 6.170, mypy 2.4, Ruff 0.17, import-linter 2.15, pip-audit 2.10. The exact versions are in `requirements.lock` and `requirements-dev.lock`.
