# payments-secure-open-banking

An open-banking style payment against a mock bank: a customer authenticates strongly at the bank and gives a merchant a **consent** (a payee, a cap, a scope and an expiry); the merchant, authenticated to the bank with a token bound to its own key, asks for a payment under that consent; and the bank moves money only if every rule of the consent holds, exactly once, for the amount that was signed.

| | |
| --- | --- |
| **Lesson** | `/prep/payments/project-secure-open-banking`, "An open-banking style payment" (`data-structure-explainer/src/content/tracks/payments/14-project-secure-open-banking.md` in the Plainround app) |
| **Planned hours** | 14 |
| **Level** | Advanced |
| **Architecture** | [ARCHITECTURE.md](ARCHITECTURE.md) (with the trust diagram), decisions: [docs/adr](docs/adr) |
| **Technology notes** | [tech/](tech/README.md): one beginner note per technology used here |

Everything is a local simulation with fake accounts; no real bank, money or personal data is involved.

## Architecture

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/architecture-dark.svg">
  <img alt="The Customer authenticates at Bank login, which sends a code to the Mock bank's phone and creates a consent in Accounts and consents. The Merchant service takes no part in that; it calls the Payment API with a key-bound token and a signed request, and the Payment API posts the payment once." src="docs/diagrams/architecture.svg">
</picture>

The customer's authentication and the merchant's authentication are separate paths into the same bank, and only the bank decides, so the merchant's code cannot reach the bank's login or consent code.

## How one run flows

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/flow-dark.svg">
  <img alt="The merchant gets a key-bound token, signs the request with its idempotency key, and the bank authenticates it, checks the signature before the consent, checks every rule, posts once and signs the answer." src="docs/diagrams/flow.svg">
</picture>

The amber row is a changed amount, refused with request_not_as_signed before the consent is read; the green box is the signed, once-only payment.

## Prerequisites

| Tool | Version | Used for |
| --- | --- | --- |
| Docker with Compose v2 | 24 or newer (tested with 24.0.6) | PostgreSQL, and the optional bank image |
| Python | 3.12 or 3.13 (tested with 3.13; the CI uses 3.12) | the bank, the merchant, the programs and the tests |
| GNU Make and OpenSSL | Make 3.81 or newer; `openssl` (preinstalled on macOS) | every command below; `make up` uses `openssl rand` once to create the bank's pepper |
| PostgreSQL image | `postgres:18` | pulled on first `make up` |
| Python packages | `fastapi`, `uvicorn`, `psycopg[binary,pool]` 3.2 or newer, `httpx`, `pyjwt[crypto]` at run time; `pytest`, `pytest-cov`, `hypothesis`, `ruff`, `mypy`, `import-linter`, `pip-audit` for development. All pinned with hashes in `requirements.lock` and `requirements-dev.lock`; `make` installs them into `.venv/` | everything |

No cloud account. Secrets come from the environment or from files readable by their owner, never from code: `make up` creates `.env` from `.env.example` the first time and generates `BANK_PEPPER` (the key for the digests of codes and tokens); the database password is a throwaway value for a database that lives on this machine only. Merchant keys and secrets and customer PINs are created by `make register` (or by `make run`) with random values under `var/`, which git ignores; none is in the repository, the seed or a log. Ports 54613 (PostgreSQL), 8463 (the bank) and 8464 (the merchant) must be free; they are set in `.env`.

## Setup, run, test, stop

```bash
make up                  # PostgreSQL (and .env with a generated pepper), then sql/001_schema.sql and sql/002_seed.sql
make run                 # the lesson's scenario: a bank and a merchant server in this process, every step and every failure
make queries             # sql/003_queries.sql: balances, the zero-sum check, rule checks that must return no rows
make register            # merchant-1 with a key and secret, customers ada and bob with random PINs, under var/
make serve-bank          # the bank on http://127.0.0.1:8463 (Ctrl-C stops it gracefully)
make serve-merchant      # the merchant on http://127.0.0.1:8464 (after make register)
make health              # exit 0 when the running bank reports healthy
make check               # format check, lint, types, import rules, unit tests with coverage (no services needed)
make test-integration    # the database rules and the races against PostgreSQL (needs make up; resets the database)
make test-e2e            # the scenario against real bank and merchant processes (needs make up; resets the database)
make audit               # known-vulnerability check of the locked dependencies
make migrate             # the stepwise migrations applied to a scratch database
make down                # stops the container and deletes its data
```

`make help` lists every target. `make run` starts with `make db`, which drops and recreates the schema, so each run is independent; `make test-integration` and `make test-e2e` do the same, so do not point them at a database you care about. Operational log lines (structured `logfmt`) go to standard error; results go to standard output.

### The pieces by hand (separate processes)

```bash
make register                                   # writes var/merchant-1.env, var/merchant-1-key.pem, var/ada.pin, var/bob.pin
make serve-bank                                 # terminal 1
make serve-merchant                             # terminal 2
curl -s -X POST localhost:8464/v1/checkouts -H 'Content-Type: application/json' \
  -d '{"amount_minor": 3000, "currency": "USD"}'                      # the checkout and the terms to approve
set -a; . ./.env; set +a
.venv/bin/open-banking-customer approve --username ada --account acct-ada --payee acct-shop \
  --merchant merchant-1 --max 30.00 --pin-file var/ada.pin --outbox var/otp-outbox.log   # strong authentication; prints consent_id
curl -s -X POST localhost:8464/v1/checkouts/<checkout_id>/complete -H 'Content-Type: application/json' \
  -d '{"consent_id": "<consent_id>"}'                                  # PAID; run it again: the same payment
.venv/bin/open-banking-customer revoke --username ada --consent-id <consent_id> --pin-file var/ada.pin
.venv/bin/open-banking-admin balances acct-ada acct-shop
```

### The bank's API

| Method and path | Who | What it does |
| --- | --- | --- |
| `POST /v1/sca/challenges` | customer | `{"username", "pin", "terms"}`: checks the PIN, sends a one-time code to the customer's device, for exactly these terms. |
| `POST /v1/sca/challenges/{id}/verify` | customer | `{"code"}`: a right code returns a single-use proof (2 minutes); a wrong one counts; the third locks the challenge. |
| `POST /v1/consents` | customer | `Authorization: SCA <proof>` and `{"terms"}`: creates the consent. Without a valid proof for these terms: `401`. |
| `POST /v1/consents/{id}/revoke` | customer | `{"username", "pin"}`: withdraws a consent. |
| `POST /oauth/token` | merchant | Client secret (Basic) and a `DPoP` proof: a token bound to the merchant's key (10 minutes). |
| `POST /v1/payments` | merchant | `Authorization: DPoP <token>`, a fresh `DPoP` proof, `Idempotency-Key`, `X-Request-Signature`, and `{"consent_id", "payee_account", "amount_minor", "currency"}`: `201` with the payment and a `Bank-Signature`, or one named refusal. |
| `GET /v1/payments/{id}` | merchant | A payment the merchant made. |
| `GET /.well-known/bank-keys.json`, `GET /health`, `GET /health/live` | anyone | The key results are signed with; dependency health (`503` when the database is down); liveness. |

Errors have one shape, `{"error": {"code", "message"}}`. Refusals of a payment each have their own code: `consent_not_found` (404), `request_not_as_signed` (400), `consent_not_granted_to_caller`, `payee_not_covered`, `consent_revoked`, `consent_expired`, `consent_already_used`, `currency_not_covered`, `amount_exceeds_consent`, `period_cap_exceeded` (403) and `insufficient_funds` (422).

The merchant's API: `POST /v1/checkouts` (the amount; the answer names the terms the customer must approve), `POST /v1/checkouts/{id}/complete` (the consent id the customer brought back: `200` and `PAID`, `403` with the bank's reason, `504` with `retryable: true` when the bank could not be reached), `GET /v1/checkouts/{id}`, `GET /health`.

## The SQL

Every statement the project runs against PostgreSQL is a file under [sql/](sql/), run in this order:

| File | Kind | What it holds | Run by |
| --- | --- | --- | --- |
| [001_schema.sql](sql/001_schema.sql) | DDL | `bank_accounts`, `journal_entries`, `postings`, `customers`, `authentication_challenges`, `authentication_proofs`, `consents`, `oauth_clients`, `access_tokens`, `seen_proofs`, `payments`, `idempotency_keys`; keys, indexes, `CHECK` constraints; the deferred triggers that make an unbalanced entry and a balance that disagrees with its postings impossible; the triggers that make the ledger append-only | `make up` (`make db`) |
| [002_seed.sql](sql/002_seed.sql) | DML | the lesson's `acct-ada` (500.00) and `acct-shop` (0.00), plus `acct-bob` (200.00), `acct-other-shop` and a vault that funds them through balanced entries. No customer, merchant or credential | `make up` (`make db`) |
| [003_queries.sql](sql/003_queries.sql) | queries | balances and the zero-sum check, balance against postings, unbalanced entries, consents live/used/revoked/expired, single-payment overuse, payments without a ledger entry, payments over their consent (the last five return no rows), recurring spend per window, payments per merchant, keys, challenges by status, proofs | `make queries` (also at the end of `make run`) |
| [migrations/001_accounts_and_ledger.sql](sql/migrations/001_accounts_and_ledger.sql) to [004_payments.sql](sql/migrations/004_payments.sql) | DDL (the lesson's steps) | the starting point (accounts and the ledger); steps 1 and 2 (customers, authentication, consent); step 3 (merchants, tokens, proofs of possession); steps 4 to 6 (payments, idempotency keys) | `make migrate` (to a scratch database); the integration test proves the result equals `001_schema.sql` |
| [reset.sql](sql/reset.sql) | DDL (outside the run order) | drop the schema; the ledger is append-only, so a reset cannot be a `DELETE` | `make db`; the test fixtures |

## Done when: the lesson's checklist and the proof for each line

| # | Done when (from the lesson) | Command | What proves it |
| --- | --- | --- | --- |
| 1 | The diagram shows where strong customer authentication happens and where the merchant is authenticated to the bank. | read [ARCHITECTURE.md](ARCHITECTURE.md); `make run` (section 1); `make lint` | The sequence diagram has a box around the customer-to-bank authentication (no merchant on that path) and a separate box where the merchant authenticates with a secret and a key-bound token. `make run` prints the three trust relationships. `make lint` runs an import-linter contract that makes the merchant's modules unable to import the bank's authentication, consent or payment code, so the claim is checked, not just drawn (it caught a real coupling while the project was built, ADR 0005). |
| 2 | A consent exists only after strong authentication, and it carries a scope and an expiry you can inspect. | `make run` (section 2) | `consent without authentication -> 401 invalid_authentication_proof: consent requires strong customer authentication first`; a wrong PIN `-> 401`; an abandoned authentication leaves `consents before and after: 0 and 0`; three wrong codes `-> wrong_code, wrong_code, challenge_locked`; a proof for 50.00 used for 5,000.00 `-> 401 ... issued for different terms`; then `consent_id=..., scope=single-payment, max=50.00, expires_at=... (10 minutes from now)`; the same proof again `-> 401 ... already used`. `make queries` shows the scope and expiry columns; `Consent.grant` cannot be called without a proof. |
| 3 | A payment request without a valid merchant credential is refused, and one that references another customer's consent is refused. | `make run` (section 3) | `no merchant credential -> 401`; `a stolen token used with another key -> 401 ... signed with a different key`; `a wrong client secret ... -> 401`; `a proof of possession used twice -> 200, then 401 ... already used`; and `another merchant presents this consent -> consent_not_granted_to_caller: consent was not granted to this merchant`, although that consent is real and unexpired. (Here a consent is granted to one merchant; a consent for another customer's payment names another payee or another merchant, and both are refused with their own reasons.) |
| 4 | A payment within consent succeeds, and payments over the consent's amount, to a different payee, or after expiry are each refused with a clear reason. | `make run` (section 4) | `amount=60.00 -> 403 amount_exceeds_consent: amount 60.00 exceeds consent max 50.00`; `payee_account=acct-other-shop -> 403 payee_not_covered: consent does not cover this payee`; `amount=30.00, correct payee -> Success`, `ada 500.00 -> 470.00; shop 0.00 -> 30.00; ledger sum 0.00`; `clock 1 second past expires_at -> 403 consent_expired: consent expired`. |
| 5 | A replayed initiation moves money once, and a request whose amount differs from the approved consent is rejected. | `make run` (section 5) | The same key twice: `201, payment ... posted once`, then `201, same payment ..., Idempotent-Replay=true, ada debited 3000 minor units in total`; a new key under a used single-payment consent `-> consent_already_used`; `amount=35.00, signature covers amount=30.00 -> 400 request_not_as_signed: amount does not match approved consent: the request signature does not cover this request`, with no money moved. |
| 6 | An abandoned or revoked consent moves no money, and a timeout reconciles to one payment or none, never two. | `make run` (section 6) | `abandoned authentication: no consent exists; initiate against a fabricated id -> 404 consent_not_found`; `revoked consent: revoke -> 200; initiate -> 403 consent_revoked: consent revoked`, balances unchanged; `bank timeout: the bank posted the payment, the answer was lost, the merchant retried with the same key -> PAID`; and with every answer lost `-> BankUnavailableError, checkout UNKNOWN`, then `completing again later -> PAID; ada debited 2000 minor units: one payment, not two`. |

`make run` finishes with the books: `ledger sum across every posting: 0.00` and no money created or lost. The same scenarios run as automated tests: the unit tests (domain rules, the signed-request and proof checks, and a property test over random sequences of grants, payments, revocations, clock moves and tampering), the integration tests (each race against PostgreSQL) and the end-to-end tests (real bank and merchant processes, the customer and admin programs, SIGTERM, and a check that no log holds a secret).

## Verification status

Verified on 2026-10-27 and 2026-11-02, one stack at a time, each taken down afterwards: in a fresh clone `make install`, `make check` and `make audit` (198 unit tests, 3 import contracts kept, 99% coverage of domain and application, no known vulnerabilities), `make up`, `make run` (all eight sections; the last line is `ledger sum across every posting: 0.00`), `make queries`, `make test-integration` (35 passed, including the concurrency races) and `make docker-build`; then `make test-e2e` (7 passed). Re-run on 2026-11-02 after the technology notes were rewritten: `make check` (198 unit tests, lint, types and 3 import contracts clean), then with the database up `make test-integration` (35 passed), `make test-e2e` (7 passed), `make migrate`, `make queries`, `make run` (ending `ledger sum across every posting: 0.00`), `make register`, `make serve-bank` with `make health` and the published bank key, and `make down`.

The first end-to-end run had one failure, a test-isolation fault: the demo test ran on a database in which an earlier test of the module had already enrolled the customer `ada`. The demo test now reloads the schema first.

## Stretch goals

| Stretch (from the lesson) | Built? |
| --- | --- |
| Add a recurring consent with a cap over a period and enforce the period limit. | Yes. `scope: recurring` with `period_seconds`; `make run` (section 7): `recurring, cap 100.00 per 30 days: pay 60.00 -> 201; pay 50.00 more -> 403 period_cap_exceeded: ... total to 110.00, over the consent cap 100.00`, and 31 days later the window has moved and 50.00 passes. |
| Add consent revocation by the customer and show an in-flight payment respecting it. | Yes. Revocation was needed for step 6; the in-flight part: `revocation in flight, 12 races: consistent 12, inconsistent 0` (a payment and a revocation sent at the same moment: either the payment came first, with a time at or before the revocation, or it is refused as revoked; never a payment after). The integration test runs the same race against PostgreSQL. |
| Verify a signed callback from the bank confirming the payment result. | Partly. The bank signs every payment result (header `Bank-Signature`, ES256, key at `/.well-known/bank-keys.json`) and the merchant's client verifies it (`the merchant verified the bank's signature on the result of payment ...`). A callback the bank initiates (a webhook) is not built; ARCHITECTURE.md says how it would reuse the same signature. |

## What is simplified for teaching

Only what the lesson scopes out, plus where this build differs from the lesson's text:

- **The bank is a mock** and the customer's "redirect" is a command: the customer program stands in for the bank's page.
- **The second factor is a file** readable by its owner (the lesson allows "a code sent to a log file or a hardcoded OTP"); the code is random per challenge, never stored in clear and never in a log. The first factor is a PIN, enrolled with a random value.
- **Dynamic linking:** the lesson's receipt over `(consent_id, payee_account, max_amount)` is replaced by two stronger mechanisms: the proof of authentication is tied to the terms (so the customer's approval cannot be moved to other terms), and the merchant's request is signed with its registered key (ADR 0003).
- **The tamper check** is a signature over the whole request, not only the amount; its refusal message is the lesson's "amount does not match approved consent", with the reason it can actually know.
- **Sender-constrained tokens** follow DPoP (RFC 9449) with an opaque token in the database instead of a signed one.
- The merchant keeps its checkouts **in memory** and has no database; it holds only its own key and secret.
- Plain HTTP on localhost, no TLS; one process per service, no authentication on `GET /health`.
- The lesson stores `NUMERIC(12,2)` balances; here money is integer minor units with a currency, and the database checks every stored balance against the sum of its postings at commit.
- **The database is disposable.** `docker-compose.yml` runs PostgreSQL with `fsync`, `synchronous_commit` and `full_page_writes` off, so the tests and demos do not wait on disk and a crash recovers at once. Never use those settings for data you want to keep.
- The `Dockerfile` packages both programs (non-root, pinned base image, the health command as its check) and is built by `make docker-build`; `docker compose --profile app up --build` starts the bank beside PostgreSQL.

## Project structure

```text
src/open_banking/
  domain/            money, the transfer, consent (terms, proof, the refusal rules), the challenge (two factors, three
                     attempts), the signed request, idempotency keys, named errors. Standard library only.
  application/       ports.py (the clock and ids both sides share)
    bank/            the bank's use cases (authentication, consent, merchant access, payments, enrollment, queries) and its
                     ports. Depends on domain only.
    merchant/        the checkout, the BankGateway port and in-memory checkouts. Cannot import bank/ (checked by lint).
  infrastructure/    adapters: psycopg pool, unit of work and ten repositories; the cryptography (scrypt, keyed digests,
                     proofs of possession, signatures); the stand-in phone; the merchant's HTTP client and its fault layer;
                     settings (shared and merchant, bank); logging; retry; clocks. Depends on application and domain.
  interfaces/        the bank and merchant FastAPI applications and servers, their two composition roots, the admin,
                     customer and demo programs, the health command. Nothing depends on it.
sql/                 001_schema, 002_seed, 003_queries, reset, migrations/001..004.
tests/
  unit/              domain, application (the whole bank on an in-memory unit of work, real cryptography), infrastructure,
                     HTTP through the test client, the merchant's client against the bank app. No services.
  integration/       the schema's rules, the migrations against the schema, and the flows and races on PostgreSQL.
  e2e/               real bank and merchant processes, the customer and admin programs, SIGTERM, and the demo program.
docs/adr/            decision records: consent against a proof, key-bound tokens, once-only and tamper-evident, locks,
                     the merchant's isolation.
tech/                one beginner-level note per technology used here (tech/README.md is the index).
docker-compose.yml   PostgreSQL 18 (and the bank under the "app" profile).      Dockerfile  the bank and merchant as an image.
pyproject.toml       project metadata and every tool's configuration (ruff, mypy, pytest, import-linter).
requirements*.lock   hash-pinned dependencies (runtime, and runtime plus development tools); `make lock` regenerates them.
Makefile             the only commands a reader needs (make help).
```

The dependency rule (domain depends on nothing; application on domain; infrastructure and interfaces on both; nothing on interfaces) is enforced by `make lint` through import-linter. A second contract keeps PostgreSQL, the web framework and the cryptography libraries out of `domain` and `application`; a third keeps the merchant side from reaching the bank's authentication, consent and payment code.

## Author and license

Written by Sumit Srivastava. Released under the MIT License, see [LICENSE](LICENSE). Changes are in [CHANGELOG.md](CHANGELOG.md).
