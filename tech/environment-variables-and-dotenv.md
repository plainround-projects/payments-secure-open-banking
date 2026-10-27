# Environment variables and the .env file

## What it is

An **environment variable** is a named text value that the operating system gives to every program it starts (for example `HOME` or `PATH`). Programs read them to find out about their surroundings. A **`.env` file** is a plain text file of `NAME=value` lines that this project's `Makefile` loads into the environment before running commands. `.env.example` is the committed template; `.env` is your private copy and is never committed.

## Why this repository uses it

The service follows the "twelve-factor" rule that **configuration lives in the environment, not in the code**. The same program must run on a laptop, in CI and in a container, only with different values: which database, which port, and above all the **password**. A secret in source code ends up in git history; a secret in the environment does not.

## Where it appears

| Path | Role |
| --- | --- |
| `src/.../infrastructure/settings.py` | The shared helpers (`read_text`, `read_positive_int`), `ConfigurationError`, and `MerchantConfig.from_env()` for the merchant. |
| `src/.../infrastructure/bank_settings.py` | `BankConfig.from_env()`: reads and checks every bank variable once, at startup. |
| `.env.example` | The template: one line per variable, with a local-development value and no real secret. |
| `.env` | Your copy, created by `make up` the first time; ignored by git and by Docker builds. |
| `Makefile` | `-include .env` and `export` pass the variables to every command. |
| `docker-compose.yml` | `${VAR:-default}` reads them to configure the containers. |
| `.github/workflows/ci.yml` | Sets `POSTGRES_PASSWORD` for the integration job. |
| `Dockerfile` | Sets `BANK_HOST` and `BANK_PORT` defaults for the container. |

## How it works underneath

When a program starts, its parent hands it a copy of a dictionary of variables. The program reads it (`os.environ`). A child process inherits a copy. A change made inside a program is not seen by its parent. A shell sets one for a single command with `NAME=value command`.

## Walkthrough of this repository's use

### Reading and checking

```python
def read_text(environ: Mapping[str, str], name: str, default: str | None = None) -> str:
    value = environ.get(name, default if default is not None else "").strip()
    if not value:
        raise ConfigurationError(f"{name} must be set and not empty")
    return value

def read_positive_int(environ, name, default, *, maximum=None) -> int:
    raw = environ.get(name, str(default)).strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ConfigurationError(f"{name} must be a positive integer, got {raw!r}")
    ...
```

Environment values are always text. These helpers fetch a name, apply a default when there is one, and refuse nonsense. `POSTGRES_PASSWORD` has **no default**, so a missing password is a startup error, not a silent empty password.

### The variables

| Name | Default | Meaning |
| --- | --- | --- |
| `POSTGRES_HOST` | `localhost` | Database host (bank only). |
| `POSTGRES_PORT` | `54613` | Database port on the host. |
| `POSTGRES_DB`, `POSTGRES_USER` | `openbanking` | Database and role names. |
| `POSTGRES_PASSWORD` | none | Required; `make up` copies a local-development value from `.env.example`. |
| `POSTGRES_CONNECT_TIMEOUT_SECONDS` | `5` | Connect timeout. |
| `DB_POOL_MAX_SIZE` | `20` | Largest connection pool. |
| `BANK_HOST`, `BANK_PORT` | `127.0.0.1`, `8463` | Where the bank listens. |
| `BANK_PUBLIC_URL` | derived | The address merchants use for the bank; a proof of possession is bound to it. |
| `BANK_PEPPER` | none | Required, at least 16 characters; keys the digests of codes and tokens. `make up` generates it. |
| `SCRYPT_N` | `16384` | Cost of the slow hash for PINs and client secrets; a power of two. |
| `BANK_SIGNING_KEY_FILE` | `var/bank-signing-key.pem` | The bank's private signing key (created on first start, owner-only). |
| `OTP_OUTBOX_FILE` | `var/otp-outbox.log` | The file that stands in for the customer's phone. |
| `CHALLENGE_MINUTES`, `PROOF_MINUTES`, `TOKEN_MINUTES` | `5`, `2`, `10` | Lifetimes of a code challenge, a proof of authentication and a merchant token. |
| `CONSENT_MINUTES`, `RECURRING_CONSENT_DAYS` | `10`, `90` | Lifetime of a single-payment and of a recurring consent. |
| `DPOP_MAX_SKEW_SECONDS` | `60` | How far a proof's time may differ from the bank's clock. |
| `IDEMPOTENCY_KEY_TTL_HOURS` | `24` | How long a stored answer is replayable. |
| `SHUTDOWN_GRACE_SECONDS` | `10` | Wait for in-flight requests at shutdown. |
| `MERCHANT_HOST`, `MERCHANT_PORT` | `127.0.0.1`, `8464` | Where the merchant listens. |
| `MERCHANT_CLIENT_ID`, `MERCHANT_CLIENT_SECRET` | `merchant-1`, none | The merchant's credentials at the bank; the secret is required and written by `make register`. |
| `MERCHANT_PRIVATE_KEY_FILE` | `var/merchant-1-key.pem` | The merchant's private key. |
| `MERCHANT_PAYEE_ACCOUNT` | `acct-shop` | The account the merchant is paid into. |
| `BANK_TIMEOUT_SECONDS`, `BANK_RETRY_ATTEMPTS`, `BANK_RETRY_BASE_DELAY_MS` | `5`, `3`, `50` | The merchant's client: request timeout and retry policy. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

Two programs, two configurations: `BankConfig` (bank, admin and health) and `MerchantConfig` (merchant). The merchant never receives the database settings or the pepper (a unit test checks this).

### A frozen, validated object

`BankConfig` and `MerchantConfig` are frozen dataclasses of nested frozen dataclasses. The secret fields (the database password, the pepper, the merchant's client secret) are `field(repr=False)`, so printing or logging a configuration never shows them. The pepper gets one more check: `BankConfig.from_env` refuses one shorter than 16 characters, and `SCRYPT_N` must be a power of two.

### How `.env` reaches programs

```make
-include .env
export
```

Make includes `.env` (variables become make variables) and `export` hands them all to the commands it runs. So `make serve-bank`, `make test-integration`, `psql` through Compose and the Python programs all see the same values. In CI there is no `.env`; the workflow sets the variables itself.

### Creating `.env`

```make
env:
	@[ -f .env ] || { cp .env.example .env; \
	  pepper=$$(openssl rand -hex 24); sed -i.bak "s/^BANK_PEPPER=.*/BANK_PEPPER=$$pepper/" .env && rm -f .env.bak; \
	  echo "created .env from .env.example with a generated BANK_PEPPER"; }
```

Copies the template only if `.env` does not exist, so your edits are never overwritten, and replaces the pepper placeholder with 48 random hexadecimal characters (see `shell.md` and `openssl.md`). Secrets that belong to *people and merchants* (a customer's PIN, a merchant's client secret and private key) are not in `.env` at all: `make register` writes them to owner-only files under `var/`.

### Rules for secrets here

- The database password in `.env.example` is for a local, throwaway database only; the pepper is generated per checkout.
- Nothing in `src/` contains a credential.
- The logger masks secret-looking field names and card-number-shaped text.
- `.env` is in `.gitignore` and `.dockerignore`.

## Try it yourself

1. See your effective settings without the password:

   ```bash
   set -a; . ./.env; set +a; .venv/bin/python -c "from open_banking.infrastructure.bank_settings import BankConfig; print(BankConfig.from_env())"
   ```

   Expected: a `BankConfig(postgres=PostgresSettings(host='localhost', port=54613, database='openbanking', ...), server=ServerSettings(...), ...)` line with **no** password and **no** pepper in it.

2. Trigger the missing-secret error:

   ```bash
   env -u BANK_PEPPER -u POSTGRES_PASSWORD .venv/bin/python -c "from open_banking.infrastructure.bank_settings import BankConfig; BankConfig.from_env()"
   ```

   Expected (if the variables are not set in your shell): `ConfigurationError: BANK_PEPPER must be set and not empty`. The pepper is checked first; with the pepper set, the next error names `POSTGRES_PASSWORD`.

3. Override one value for one command: `BANK_PORT=9000 make serve-bank`, then `BANK_PORT=9000 make health`.

4. Bad value: `LOG_LEVEL=LOUD make serve-bank` prints `configuration error: LOG_LEVEL must be one of [...]` and exits with status 2.

## Mistakes people make

- **Committing `.env`.** It is git-ignored; commit only `.env.example`.
- **Reading variables all over the code.** Here only `settings.py` and `bank_settings.py` do, so the settings are in two files.
- **Defaulting a secret.** A default password can reach production unnoticed; here it is required.
- **Forgetting values are strings.** `"0"` is truthy as text; the helpers convert and validate.
- **Putting spaces around `=`** in a `.env` line: Make and shell parse it differently.

## Check yourself

1. Why does `POSTGRES_PASSWORD` have no default? *A missing password must fail loudly, not fall back.*
2. What does `export` do in the Makefile? *Passes every variable to the commands make runs.*
3. Which file is committed, `.env` or `.env.example`? *`.env.example`.*
4. What does `field(repr=False)` do? *Keeps the field out of the printed form of the object, so the password is not logged.*
5. Where are the settings read? *Only in `BankConfig.from_env()` and `MerchantConfig.from_env()`.*

## Further reading

- The Twelve-Factor App, III. Config: https://12factor.net/config
- Python `os.environ`: https://docs.python.org/3.12/library/os.html#os.environ
- Docker Compose environment variables: https://docs.docker.com/compose/how-tos/environment-variables/
- GNU Make `include` and `export`: https://www.gnu.org/software/make/manual/html_node/Environment.html
