# Python walkthrough: the interfaces package, line by line

## What it is

This note reads every line of the `interfaces` package: the shared HTTP pieces, the two composition roots, the bank's HTTP API, the merchant's HTTP API, the two server launchers, the health command, and the three command-line programs (the operator's admin program, the customer's program, and the demo that runs the whole lesson).

An **interface** here means the way a person or another program reaches the service. This layer contains no business rules; it translates between the outside world and the use cases.

## Why this repository uses it

The lesson ends with a full scenario, from strong authentication and consent through merchant access and payment to failure paths and a race between a payment and a revocation. The demo runs exactly that, against a real bank and merchant, and fails loudly if any step is wrong. The two HTTP APIs expose the same abilities to other programs, and the careful signal handling lets an orchestrator stop a server without seeing a crash.

## Where it appears

| File | What it is |
| --- | --- |
| `src/open_banking/interfaces/__init__.py` | The interfaces package. |
| `src/open_banking/interfaces/http_common.py` | Shared logging and error handling for both APIs. |
| `src/open_banking/interfaces/bank_container.py` | The bank's composition root. |
| `src/open_banking/interfaces/merchant_container.py` | The merchant's composition root. |
| `src/open_banking/interfaces/bank_api.py` | The bank's HTTP API. |
| `src/open_banking/interfaces/merchant_api.py` | The merchant's HTTP API. |
| `src/open_banking/interfaces/serve_bank.py` | The bank server launcher. |
| `src/open_banking/interfaces/serve_merchant.py` | The merchant server launcher. |
| `src/open_banking/interfaces/health_cli.py` | The health-check command. |
| `src/open_banking/interfaces/admin_cli.py` | The operator's program. |
| `src/open_banking/interfaces/customer_cli.py` | The customer's program. |
| `src/open_banking/interfaces/demo_cli.py` | The lesson's scenario. |

## How it works underneath

**Composition root:** one function per process that creates concrete adapters and hands them to the use cases. **FastAPI** turns functions with decorators such as `@app.post("/v1/payments")` into routes; it parses the path, headers and JSON body into typed parameters (Pydantic models check the body strictly) and writes the return value as JSON. **Exception handlers** convert raised errors into one error shape. **Middleware** wraps every request. **argparse** reads a command line into an object whose attributes are the options. A **signal** is a message the operating system sends to a process; `SIGTERM` asks it to stop politely. The demo starts both servers as threads in one process and drives them over real HTTP.

## Walkthrough of this repository's use



### `src/open_banking/interfaces/__init__.py` (1 lines)

```python
"""The bank and merchant HTTP APIs and the command-line programs. Nothing depends on this layer."""
```

The docstring of the `interfaces` package: the bank and merchant HTTP APIs and the command-line programs. It wires the adapters into the use cases. Nothing depends on this layer, which is the top of the layering.

### `src/open_banking/interfaces/http_common.py` (54 lines)

```python
"""What both HTTP applications share: request logging that never records bodies or headers, and the error shape."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from open_banking.application.logs import fields
```

What both HTTP applications share: request logging that never records bodies or headers, and the error shape. `FastAPI`, `Request`, `Response`, `RequestValidationError` and `JSONResponse` come from FastAPI; `uuid` makes request ids; `time` measures durations.

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
def error_response(code: str, message: str, status: int, **extra: Any) -> JSONResponse:
    """The one error shape: `{"error": {"code", "message", ...}}`."""
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status)
```

The one error shape for every error answer: `{"error": {"code": ..., "message": ..., ...}}`. `**extra: Any` collects any extra keyword arguments (for example `attempts_left`) into a dictionary, and `**extra` inside the dictionary literal spreads them into the error object. The status code is set on the response.

```python
def install_common(app: FastAPI) -> None:
    """Request logging (method, path, status, duration, request id; no bodies, no headers) and the catch-alls."""
```

Installs on an application the pieces both servers share. The docstring: request logging (method, path, status, duration and request id; no bodies, no headers, so no secret can reach a log) and the catch-all handlers.

```python
    @app.middleware("http")
    async def log_requests(request: Request, call_next: Callable[[Request], Any]) -> Response:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        started = time.perf_counter()
        response: Response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        logger.info(
            "request",
            extra=fields(
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            ),
        )
        return response
```

**Middleware** wraps every request. `log_requests` takes the caller's `X-Request-Id` header or makes a new id (`or` picks the second value when the first is empty), notes the start time, calls the rest of the chain (`await call_next(request)`), adds the id to the response header and writes one log line with the request id, method, path, status and the duration in milliseconds (`time.perf_counter()` is a precise stopwatch). `async def` / `await` are Python's syntax for code that can pause while waiting.

```python
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" for e in error.errors())
        return error_response("invalid_request", problems, 400)
```

Bad request data (a missing field, a wrong type) is answered with status 400 and a readable text built from the list of problems: for each, the location joined with dots and the message, separated by `; `. The code is `invalid_request`.

```python
    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        logger.error("unexpected error", extra=fields(error=type(error).__name__), exc_info=error)
        return error_response("internal_error", "an unexpected error occurred", 500)
```

The catch-all: an unexpected error is logged with its traceback (`exc_info=error`) and the client gets a fixed 500 message, so no internal detail leaks.

### `src/open_banking/interfaces/bank_container.py` (120 lines)

```python
"""The bank's composition root: its adapters wired into its use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from open_banking.application.bank.access import AuthenticateCaller, IssueToken
from open_banking.application.bank.authentication import StartChallenge, VerifyChallenge
from open_banking.application.bank.consents import GrantConsent, RevokeConsent
from open_banking.application.bank.enrollment import EnrollCustomer, RegisterClient
from open_banking.application.bank.idempotent import IdempotentExecutor
from open_banking.application.bank.payments import GetPayment, InitiatePayment
from open_banking.application.bank.ports import BankSettings, Crypto, HealthProbe, OtpChannel, UnitOfWorkFactory
from open_banking.application.bank.queries import CheckHealth, GetBalances
from open_banking.application.ports import Clock, IdSource
from open_banking.infrastructure import keys, postgres
from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.crypto import StandardCrypto
from open_banking.infrastructure.otp import FileOtpChannel
from open_banking.infrastructure.system import RandomIds, SystemClock
```

The bank's composition root: its adapters wired into its use cases. This is the one place that knows which concrete classes implement which ports. `Path` is a file path; `UUID` is the id type.

```python
@dataclass(frozen=True, slots=True)
class BankContainer:
    """The bank's use cases, the key it signs results with, and how to release resources."""

    start_challenge: StartChallenge
    verify_challenge: VerifyChallenge
    grant_consent: GrantConsent
    revoke_consent: RevokeConsent
    issue_token: IssueToken
    authenticate: AuthenticateCaller
    initiate_payment: InitiatePayment
    get_payment: GetPayment
    balances: GetBalances
    enroll_customer: EnrollCustomer
    register_client: RegisterClient
    health: CheckHealth
    crypto: Crypto
    public_url: str
    signing_pem: str
    signing_jwk: dict[str, Any]
    otp: OtpChannel
    uow_factory: UnitOfWorkFactory
    close: Callable[[], None]
```

A frozen bundle of everything the bank's HTTP layer and programs call: the use cases (start and verify a challenge, grant and revoke consent, issue a token, authenticate a caller, initiate and read a payment, balances, enroll a customer, register a merchant, health), the `crypto` object, the bank's public URL, the **signing key** the bank signs payment results with (as PEM and as a JWK), the one-time-code channel, the transaction factory and a `close` function.

```python
def assemble_bank(
    *,
    uow_factory: UnitOfWorkFactory,
    crypto: Crypto,
    otp: OtpChannel,
    clock: Clock,
    ids: IdSource,
    settings: BankSettings,
    public_url: str,
    signing_pem: str,
    probes: dict[str, HealthProbe],
    close: Callable[[], None] = lambda: None,
) -> BankContainer:
    """Wire the bank's use cases around the given ports. Tests call this with in-memory ones."""
```

Wires the use cases around the given ports; the docstring says tests call it with in-memory ones. The `*` makes all arguments keyword-only. The default `close=lambda: None` is an unnamed function that does nothing.

```python
    executor = IdempotentExecutor(uow_factory, clock, settings.idempotency_key_time_to_live)
    return BankContainer(
        start_challenge=StartChallenge(uow_factory, crypto, otp, clock, ids, settings),
        verify_challenge=VerifyChallenge(uow_factory, crypto, clock, settings),
        grant_consent=GrantConsent(uow_factory, crypto, clock, ids, settings),
        revoke_consent=RevokeConsent(uow_factory, crypto, clock),
        issue_token=IssueToken(uow_factory, crypto, clock, settings),
        authenticate=AuthenticateCaller(uow_factory, crypto, clock),
        initiate_payment=InitiatePayment(executor, uow_factory, crypto, clock, ids),
        get_payment=GetPayment(uow_factory),
        balances=GetBalances(uow_factory),
        enroll_customer=EnrollCustomer(uow_factory, crypto, ids),
        register_client=RegisterClient(uow_factory, crypto),
        health=CheckHealth(probes),
        crypto=crypto,
        public_url=public_url.rstrip("/"),
        signing_pem=signing_pem,
        signing_jwk=keys.public_jwk_of(signing_pem),
        otp=otp,
        uow_factory=uow_factory,
        close=close,
    )
```

One shared idempotent executor is built from the transaction factory, the clock and the key lifetime, and passed to `InitiatePayment`. Each use case then receives exactly the ports it needs. `public_url.rstrip("/")` removes a trailing slash; `keys.public_jwk_of(signing_pem)` derives the public half of the signing key.

```python
def load_or_create_signing_key(path: Path) -> str:
    """The bank's result-signing key: read it, or create it (readable by its owner only) on first start."""
    if path.exists():
        return keys.read_private_key(path)
    pem, _ = keys.generate_keypair()
    keys.write_private_key(path, pem)
    return pem
```

The bank's result-signing key: read it if the file exists, otherwise generate a new key pair, write the private key (readable by its owner only) and return it. So the first start creates the key and later starts reuse it.

```python
def build_bank_container(config: BankConfig, *, clock: Clock | None = None) -> BankContainer:
    """The real wiring: a PostgreSQL pool, the file-based code channel, and the standard cryptography."""
    pool = postgres.open_pool(config.postgres)
    ids = RandomIds()

    def new_id() -> UUID:
        return ids.new_id()

    return assemble_bank(
        uow_factory=postgres.PostgresUnitOfWorkFactory(pool, new_id),
        crypto=StandardCrypto(
            pepper=config.pepper, scrypt_n=config.scrypt_n, dpop_max_skew=config.timings.dpop_max_skew
        ),
        otp=FileOtpChannel(config.otp_outbox_file),
        clock=clock or SystemClock(),
        ids=ids,
        settings=config.timings,
        public_url=config.public_url,
        signing_pem=load_or_create_signing_key(config.signing_key_file),
        probes={"database": postgres.PostgresHealth(pool)},
        close=pool.close,
    )
```

The real wiring: open a PostgreSQL pool, make random ids, and call `assemble_bank` with the PostgreSQL unit-of-work factory (it receives `new_id`, a small function returning a fresh UUID), `StandardCrypto` built from the pepper, the scrypt cost and the DPoP skew, the file-based code channel, the real clock (or the one given, which the demo uses to move time), the timing settings, the public URL, the signing key, a database health probe and `pool.close` as the shutdown action.

### `src/open_banking/interfaces/merchant_container.py` (73 lines)

```python
"""The merchant's composition root: an HTTP client to the bank and its own in-memory checkouts. No database."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from open_banking.application.merchant.checkout import (
    CompleteCheckout,
    CreateCheckout,
    InMemoryCheckouts,
    MerchantProfile,
)
from open_banking.application.ports import IdSource
from open_banking.infrastructure import keys
from open_banking.infrastructure.bank_client import HttpBankGateway
from open_banking.infrastructure.faults import FaultInjectingTransport, FaultScript
from open_banking.infrastructure.settings import MerchantConfig
from open_banking.infrastructure.system import RandomIds
```

The merchant's composition root: an HTTP client to the bank and its own in-memory checkouts. No database. `httpx` is the HTTP client library.

```python
@dataclass(frozen=True, slots=True)
class MerchantContainer:
    """The merchant's use cases, the faults scriptable into its calls to the bank, and how to release resources."""

    create_checkout: CreateCheckout
    complete_checkout: CompleteCheckout
    checkouts: InMemoryCheckouts
    faults: FaultScript
    client_id: str
    close: Callable[[], None]
```

The merchant's use cases (create and complete a checkout), its checkouts, the scriptable faults (`faults`), its client id and a `close` function.

```python
def build_merchant_container(
    config: MerchantConfig,
    *,
    ids: IdSource | None = None,
    clock: Callable[..., Any] | None = None,
    bank_public_jwk: dict[str, Any] | None = None,
    inner_transport: httpx.BaseTransport | None = None,
) -> MerchantContainer:
    """The real wiring: an HTTP client to the bank (with a scriptable fault layer) and in-memory checkouts."""
```

The real wiring. All arguments after `config` are optional and keyword-only; tests and the demo pass their own ids, clock, the bank's public key and an inner transport.

```python
    faults = FaultScript()
    transport = FaultInjectingTransport(inner_transport or httpx.HTTPTransport(), faults)
    client = httpx.Client(transport=transport, timeout=config.request_timeout_seconds)
    private_pem = keys.read_private_key(config.private_key_file)
    options: dict[str, Any] = {}
    if clock is not None:
        options["clock"] = clock
```

Creates the fault script and wraps the real transport (`httpx.HTTPTransport()` unless one is given) in a `FaultInjectingTransport`, so faults can be scripted into the merchant's calls. The HTTP client uses that transport and the configured timeout. The merchant's private key is read from its file. `options` collects the clock only if one was given; `**options` later spreads it into the gateway's arguments.

```python
    gateway = HttpBankGateway(
        client,
        bank_url=config.bank_url,
        client_id=config.client_id,
        client_secret=config.client_secret,
        private_pem=private_pem,
        attempts=config.attempts,
        retry_base_delay_seconds=config.retry_base_delay_ms / 1000,
        bank_public_jwk=bank_public_jwk,
        **options,
    )
```

The gateway is built with the client, the bank's URL, the merchant's id and secret, its key, the number of attempts, the first retry delay converted from milliseconds to seconds (`/ 1000`) and, if known, the bank's public key for checking signed results.

```python
    checkouts = InMemoryCheckouts()
    id_source = ids or RandomIds()
    return MerchantContainer(
        create_checkout=CreateCheckout(checkouts, id_source),
        complete_checkout=CompleteCheckout(checkouts, gateway, MerchantProfile(config.client_id, config.payee_account)),
        checkouts=checkouts,
        faults=faults,
        client_id=config.client_id,
        close=client.close,
    )
```

The merchant's own state is kept in memory. The result wires `CreateCheckout`, `CompleteCheckout` (with a `MerchantProfile` of the client id and payee account), the checkouts, the faults, the client id and `client.close` as the shutdown action.

### `src/open_banking/interfaces/bank_api.py` (319 lines)

```python
"""The bank's HTTP API: customer authentication and consent on one side, merchant access and payments on the other."""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from open_banking.domain.authentication import AttemptOutcome
from open_banking.domain.consent import Consent, ConsentScope, ConsentTerms, PaymentRequest
from open_banking.domain.errors import (
    AccountNotFoundError,
    AccountNotOwnedError,
    AuthenticationFailedError,
    ChallengeNotFoundError,
    ClientAuthenticationError,
    ConsentNotFoundError,
    CurrencyMismatchError,
    IdempotencyKeyReusedError,
    InvalidIdempotencyKeyError,
    InvalidMoneyError,
    InvalidProofError,
    OpenBankingError,
    PaymentNotFoundError,
    PaymentRefusedError,
    UnknownClientError,
)
from open_banking.domain.idempotency import IdempotencyKey
from open_banking.domain.money import Money
from open_banking.infrastructure import keys
from open_banking.interfaces.bank_container import BankContainer
from open_banking.interfaces.http_common import error_response, install_common
```

The bank's HTTP API: customer authentication and consent on one side, merchant access and payments on the other. New imports: `base64`, `binascii` (the error type of bad base64), `json`, `Annotated` (to attach extra information to a type), `parse_qs` (reads a form body), `run_in_threadpool` (runs ordinary blocking code from async code), and Pydantic's `BaseModel`, `ConfigDict` and `Field`.

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
_STATUS: dict[type[OpenBankingError], int] = {
    InvalidMoneyError: 400,
    CurrencyMismatchError: 400,
    InvalidIdempotencyKeyError: 400,
    AuthenticationFailedError: 401,
    InvalidProofError: 401,
    ClientAuthenticationError: 401,
    AccountNotOwnedError: 403,
    UnknownClientError: 422,
    AccountNotFoundError: 404,
    ChallengeNotFoundError: 404,
    ConsentNotFoundError: 404,
    PaymentNotFoundError: 404,
    IdempotencyKeyReusedError: 422,
}
```

The table from each named error to its HTTP status: invalid money, a currency mismatch and a bad idempotency key are 400; a failed authentication, an invalid proof and a failed merchant credential are 401; an account that is not the customer's is 403; an unknown merchant and a reused key are 422; unknown accounts, challenges, consents and payments are 404. Any other error is 500.

```python
_REFUSAL_STATUS = {"consent_not_found": 404, "insufficient_funds": 422, "request_not_as_signed": 400}
```

The status for the payment refusals that are not the default 403: an unknown consent 404, insufficient funds 422, and a request not as signed 400.

```python
_OUTCOME_CODE = {
    AttemptOutcome.WRONG_CODE: ("wrong_code", 401),
    AttemptOutcome.LOCKED: ("challenge_locked", 401),
    AttemptOutcome.EXPIRED: ("challenge_expired", 401),
    AttemptOutcome.ALREADY_USED: ("challenge_already_used", 409),
}
```

How a failed code entry is reported: a pair of (error code, status) for each outcome: a wrong code 401 `wrong_code`, a locked challenge 401 `challenge_locked`, an expired one 401 `challenge_expired`, and a used one 409 `challenge_already_used`.

```python
class TermsBody(BaseModel):
    """The terms a customer approves."""

    model_config = ConfigDict(extra="forbid")

    payer_account: str = Field(min_length=1, max_length=64)
    payee_account: str = Field(min_length=1, max_length=64)
    client_id: str = Field(min_length=1, max_length=64)
    max_amount_minor: int = Field(strict=True, gt=0, le=10**12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    scope: ConsentScope
    period_seconds: int | None = Field(default=None, strict=True, gt=0, le=366 * 86400)
```

The terms a customer approves, as a **Pydantic model**: a class whose fields are checked when a request body is read. `ConfigDict(extra="forbid")` rejects unknown fields. Each `Field` states limits: text of 1 to 64 characters; `max_amount_minor` an integer (`strict=True` refuses `"5000"` or `50.5`), greater than 0 and at most 10**12; `currency` three capital letters (`pattern`); `scope` one of the consent scopes; `period_seconds` optional, a strict positive integer of at most a leap year.

```python
    def to_terms(self) -> ConsentTerms:
        """The domain value."""
        return ConsentTerms(
            self.payer_account,
            self.payee_account,
            self.client_id,
            Money(self.max_amount_minor, self.currency),
            self.scope,
            None if self.period_seconds is None else timedelta(seconds=self.period_seconds),
        )
```

Converts the checked body into the domain's `ConsentTerms` (building the `Money` and the optional `timedelta`).

```python
class ChallengeBody(BaseModel):
    """`POST /v1/sca/challenges`."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    pin: str = Field(min_length=1, max_length=64, repr=False)
    terms: TermsBody
```

`POST /v1/sca/challenges`: a username, a PIN (`repr=False` keeps it out of the printed form of the object) and the terms.

```python
class VerifyBody(BaseModel):
    """`POST /v1/sca/challenges/{id}/verify`."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^\d{6}$", repr=False)
```

`POST /v1/sca/challenges/{id}/verify`: a code that must be exactly six digits (`^\d{6}$`), also hidden from `repr`.

```python
class ConsentBody(BaseModel):
    """`POST /v1/consents`."""

    model_config = ConfigDict(extra="forbid")

    terms: TermsBody
```

`POST /v1/consents`: the terms.

```python
class RevokeBody(BaseModel):
    """`POST /v1/consents/{id}/revoke`."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    pin: str = Field(min_length=1, max_length=64, repr=False)
```

`POST /v1/consents/{id}/revoke`: the username and PIN.

```python
class PaymentBody(BaseModel):
    """`POST /v1/payments`."""

    model_config = ConfigDict(extra="forbid")

    consent_id: str = Field(min_length=1, max_length=64)
    payee_account: str = Field(min_length=1, max_length=64)
    amount_minor: int = Field(strict=True, gt=0, le=10**12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
```

`POST /v1/payments`: the consent id, the payee account, the amount in minor units and the currency.

```python
def consent_view(consent: Consent) -> dict[str, Any]:
    """A consent as the API shows it."""
    terms = consent.terms
    return {
        "consent_id": consent.consent_id,
        "payer_account": terms.payer_account,
        "payee_account": terms.payee_account,
        "client_id": terms.client_id,
        "max_amount_minor": terms.max_amount.minor,
        "currency": terms.max_amount.currency,
        "scope": terms.scope.value,
        "period_seconds": None if terms.period is None else int(terms.period.total_seconds()),
        "created_at": consent.created_at.isoformat(),
        "expires_at": consent.expires_at.isoformat(),
        "revoked_at": None if consent.revoked_at is None else consent.revoked_at.isoformat(),
    }
```

A consent as the API shows it: ids, accounts, the maximum, the scope, the period in seconds or `None`, and the times as ISO text (`revoked_at` is `None` until revoked).

```python
def create_bank_app(container_factory: Callable[[], BankContainer]) -> FastAPI:
    """Build the application. The container is created at startup and released at shutdown."""
```

Builds the application around a container factory. The container is created at startup and released at shutdown.

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container_factory()
        logger.info("started")
        yield
        app.state.container.close()
        logger.info("shutdown complete")

    app = FastAPI(title="Mock bank", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    install_common(app)
```

The **lifespan**: code that runs once at start (before `yield`) and once at stop (after). At start it calls the factory and stores the container on `app.state`; at stop it closes it and logs `shutdown complete`. `FastAPI(..., docs_url=None, redoc_url=None)` switches off the built-in documentation pages, and `install_common(app)` adds the shared logging and catch-all handlers.

```python
    def bank(request: Request) -> BankContainer:
        result: BankContainer = request.app.state.container
        return result
```

A helper that fetches the container from the application's state.

```python
    @app.exception_handler(PaymentRefusedError)
    async def refused(request: Request, error: PaymentRefusedError) -> JSONResponse:
        return error_response(error.refusal, error.message, _REFUSAL_STATUS.get(error.refusal, 403))
```

A refused payment is answered with the refusal code as the error code and the status from `_REFUSAL_STATUS` (default 403).

```python
    @app.exception_handler(OpenBankingError)
    async def named_error(request: Request, error: OpenBankingError) -> JSONResponse:
        status = _STATUS.get(type(error), 500)
        response = error_response(error.code, error.message, status)
        if status == 401 and isinstance(error, ClientAuthenticationError):
            response.headers["WWW-Authenticate"] = 'DPoP error="invalid_token"'
        return response
```

Any other named error is answered with its code and message and the status from the table (500 if unlisted). A failed merchant credential also gets the header `WWW-Authenticate: DPoP error="invalid_token"`, which tells a client what scheme is expected.

```python
    def caller(request: Request, authorization: str | None, dpop: str | None) -> str:
        """Authenticate a merchant request: its token, and a fresh proof it holds the token's key."""
        url = f"{bank(request).public_url}{request.url.path}"
        return bank(request).authenticate.execute(authorization, dpop, method=request.method, url=url)
```

Authenticates a merchant request: it rebuilds the URL the merchant should have signed (the bank's configured public URL plus the request path, never the `Host` header, which a client controls) and asks `AuthenticateCaller` for the merchant's client id.

```python
    @app.post("/oauth/token")
    async def token(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        dpop: Annotated[str | None, Header(alias="DPoP")] = None,
    ) -> JSONResponse:
        # The body is a standard form (application/x-www-form-urlencoded); one field needs no multipart library.
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"), keep_blank_values=True)
        grant_type = form.get("grant_type", [""])[0]
        client_id, secret = _basic_credentials(authorization)
        if grant_type != "client_credentials" or not dpop:
            raise ClientAuthenticationError("invalid merchant credential")
        url = f"{bank(request).public_url}/oauth/token"
        issued = await run_in_threadpool(bank(request).issue_token.execute, client_id, secret, dpop, url)
        return JSONResponse(
            {"access_token": issued.access_token, "token_type": "DPoP", "expires_in": issued.expires_in_seconds},
            headers={"Cache-Control": "no-store"},
        )
```

`POST /oauth/token`, the token endpoint. The header parameters are declared with `Annotated[str | None, Header(...)]` (`alias="DPoP"` gives the real header name). The body is a standard form (`application/x-www-form-urlencoded`), read by hand with `parse_qs` because one field needs no multipart library. `grant_type` must be `client_credentials`, a proof header must be present, and the credentials come from the HTTP Basic header via `_basic_credentials`. The blocking work runs in a thread pool (`run_in_threadpool`) so it does not stall the async server. The answer carries the token, its type `DPoP`, its lifetime, and `Cache-Control: no-store` so nothing keeps a copy.

```python
    @app.post("/v1/sca/challenges", status_code=201)
    def start_challenge(body: ChallengeBody, request: Request) -> JSONResponse:
        started = bank(request).start_challenge.execute(body.username, body.pin, body.terms.to_terms())
        return JSONResponse(
            {"challenge_id": started.challenge_id, "expires_at": started.expires_at.isoformat()}, status_code=201
        )
```

`POST /v1/sca/challenges`: starts a challenge (PIN checked, code sent) and answers 201 with the challenge id and expiry.

```python
    @app.post("/v1/sca/challenges/{challenge_id}/verify")
    def verify_challenge(challenge_id: str, body: VerifyBody, request: Request) -> JSONResponse:
        result = bank(request).verify_challenge.execute(challenge_id, body.code)
        if result.outcome is AttemptOutcome.VERIFIED:
            assert result.proof is not None
            assert result.proof_expires_at is not None
            return JSONResponse(
                {"authentication_proof": result.proof, "expires_at": result.proof_expires_at.isoformat()},
                headers={"Cache-Control": "no-store"},
            )
        code, status = _OUTCOME_CODE[result.outcome]
        return error_response(
            code, f"the code was not accepted ({code.replace('_', ' ')})", status, attempts_left=result.attempts_left
        )
```

`POST /v1/sca/challenges/{id}/verify`: enters the code. A verified challenge returns the **authentication proof** (once, `no-store`) and its expiry. Any other outcome is turned into an error with the code and status from `_OUTCOME_CODE` and the number of attempts left (`code.replace('_', ' ')` makes the message readable).

```python
    @app.post("/v1/consents", status_code=201)
    def grant_consent(
        body: ConsentBody, request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> JSONResponse:
        scheme, _, proof = (authorization or "").partition(" ")
        if scheme != "SCA" or not proof:
            raise InvalidProofError("consent requires strong customer authentication first")
        consent = bank(request).grant_consent.execute(proof, body.terms.to_terms())
        return JSONResponse(consent_view(consent), status_code=201)
```

`POST /v1/consents`: creates a consent. The `Authorization` header must be `SCA <proof>` (the scheme is split off with `partition`); without it the answer is `InvalidProofError` (401). The proof and the terms go to `GrantConsent`; the answer is 201 with the consent.

```python
    @app.post("/v1/consents/{consent_id}/revoke")
    def revoke_consent(consent_id: str, body: RevokeBody, request: Request) -> JSONResponse:
        return JSONResponse(consent_view(bank(request).revoke_consent.execute(body.username, body.pin, consent_id)))
```

`POST /v1/consents/{id}/revoke`: the customer withdraws a consent with username and PIN.

```python
    @app.post("/v1/payments", status_code=201)
    def initiate_payment(
        body: PaymentBody,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        dpop: Annotated[str | None, Header(alias="DPoP")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        signature: Annotated[str | None, Header(alias="X-Request-Signature")] = None,
    ) -> JSONResponse:
        client_id = caller(request, authorization, dpop)
        if idempotency_key is None:
            raise InvalidIdempotencyKeyError("the Idempotency-Key header is required on every payment")
        result = bank(request).initiate_payment.execute(
            client_id,
            IdempotencyKey(idempotency_key),
            PaymentRequest(body.consent_id, body.payee_account, Money(body.amount_minor, body.currency)),
            signature,
        )
        payload = dict(result.response.body)
        headers = {"Bank-Signature": keys.sign(bank(request).signing_pem, _canonical(payload))}
        if result.replayed:
            headers["Idempotent-Replay"] = "true"
        return JSONResponse(payload, status_code=result.response.status, headers=headers)
```

`POST /v1/payments`, the heart of the API. Four headers are declared: `Authorization`, `DPoP`, `Idempotency-Key` and `X-Request-Signature`. First the caller is authenticated (token, key binding, fresh proof). A missing idempotency key is `InvalidIdempotencyKeyError`. Then `InitiatePayment` runs with the merchant, the key, the request and the signature. The stored body is copied; the bank signs it with its own key (`Bank-Signature`, over the canonical JSON), so the merchant can verify the answer came from the bank; a replay adds `Idempotent-Replay: true`. The status is the stored one (201 for a posted payment).

```python
    @app.get("/v1/payments/{payment_id}")
    def get_payment(
        payment_id: str,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        dpop: Annotated[str | None, Header(alias="DPoP")] = None,
    ) -> JSONResponse:
        client_id = caller(request, authorization, dpop)
        payload = bank(request).get_payment.execute(client_id, payment_id)
        return JSONResponse(
            payload, headers={"Bank-Signature": keys.sign(bank(request).signing_pem, _canonical(payload))}
        )
```

`GET /v1/payments/{id}`: a merchant reads a payment it initiated; the caller is authenticated the same way and the answer is signed.

```python
    @app.get("/.well-known/bank-keys.json")
    def bank_keys(request: Request) -> dict[str, Any]:
        return {"keys": [bank(request).signing_jwk]}
```

`GET /.well-known/bank-keys.json`: publishes the bank's public signing key as a JWK set, so merchants can fetch the key to verify results.

```python
    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}
```

`/health/live` only says the process is up.

```python
    @app.get("/health")
    def health(request: Request) -> JSONResponse:
        checks = bank(request).health.execute()
        healthy = all(value == "ok" for value in checks.values())
        return JSONResponse(
            {"status": "ok" if healthy else "unavailable", "checks": checks}, status_code=200 if healthy else 503
        )
```

`/health` asks the dependencies through the health use case and answers 200 `ok` when all are `ok`, else 503 `unavailable` with the checks.

```python
    return app
```

`create_bank_app` returns the finished application.

```python
def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
```

The canonical bytes of a JSON answer: sorted keys, no spaces, UTF-8. The bank signs these and the merchant recomputes them.

```python
def _basic_credentials(authorization: str | None) -> tuple[str, str]:
    """Read `Authorization: Basic base64(client_id:secret)`; anything else is an invalid merchant credential."""
    scheme, _, encoded = (authorization or "").partition(" ")
    if scheme.lower() != "basic" or not encoded:
        raise ClientAuthenticationError("invalid merchant credential")
    try:
        client_id, _, secret = base64.b64decode(encoded, validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise ClientAuthenticationError("invalid merchant credential") from error
    return client_id, secret
```

Reads `Authorization: Basic base64(client_id:secret)`: the scheme is compared in lower case, the text after the space is decoded with strict base64 (`validate=True`) and split at the first `:` into client id and secret. Anything else, including bad base64 or bytes that are not UTF-8, is an invalid merchant credential.

### `src/open_banking/interfaces/merchant_api.py` (126 lines)

```python
"""The merchant's HTTP API: start a checkout, and complete it with the consent id the customer brought back."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from open_banking.application.merchant.checkout import (
    BankUnavailableError,
    Checkout,
    CheckoutNotFoundError,
)
from open_banking.domain.errors import ClientAuthenticationError, InvalidMoneyError, OpenBankingError
from open_banking.domain.money import Money
from open_banking.interfaces.http_common import error_response, install_common
from open_banking.interfaces.merchant_container import MerchantContainer
```

The merchant's HTTP API: start a checkout, and complete it with the consent id the customer brought back. It uses the same FastAPI and Pydantic pieces as the bank's API.

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
_STATUS: dict[type[OpenBankingError], int] = {
    InvalidMoneyError: 400,
    CheckoutNotFoundError: 404,
    BankUnavailableError: 504,
    ClientAuthenticationError: 502,
}
```

Statuses for the merchant's errors: invalid money 400, a checkout that does not exist 404, an unreachable bank 504 (gateway timeout), and a bank that rejected the merchant's own credential 502 (bad gateway).

```python
class CheckoutBody(BaseModel):
    """`POST /v1/checkouts`."""

    model_config = ConfigDict(extra="forbid")

    amount_minor: int = Field(strict=True, gt=0, le=10**12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
```

`POST /v1/checkouts`: an amount in minor units (strict integer, greater than 0, at most 10**12) and a currency of three capital letters; unknown fields are refused.

```python
class CompleteBody(BaseModel):
    """`POST /v1/checkouts/{id}/complete`."""

    model_config = ConfigDict(extra="forbid")

    consent_id: str = Field(min_length=1, max_length=64)
```

`POST /v1/checkouts/{id}/complete`: the consent id the customer brought back from the bank.

```python
def checkout_view(checkout: Checkout, container: MerchantContainer, payee_account: str) -> dict[str, Any]:
    """A checkout, with the terms the customer must approve at the bank."""
    return {
        "checkout_id": checkout.checkout_id,
        "status": checkout.status.value,
        "amount_minor": checkout.amount.minor,
        "currency": checkout.amount.currency,
        "amount": checkout.amount.format(),
        "consent_id": checkout.consent_id,
        "payment_id": checkout.payment_id,
        "refusal": None
        if checkout.refusal is None
        else {"code": checkout.refusal.code, "message": checkout.refusal.message},
        "consent_request": {
            "client_id": container.client_id,
            "payee_account": payee_account,
            "max_amount_minor": checkout.amount.minor,
            "currency": checkout.amount.currency,
            "scope": "single-payment",
        },
    }
```

A checkout as the API shows it, including `consent_request`: the terms the customer must approve at the bank (the merchant's client id, its payee account, the maximum equal to the checkout amount, the currency and the scope `single-payment`). The expression `None if checkout.refusal is None else {...}` shows the bank's last refusal if there is one.

```python
def create_merchant_app(container_factory: Callable[[], MerchantContainer], payee_account: str) -> FastAPI:
    """Build the application. The container is created at startup and released at shutdown."""
```

Builds the application around a container factory and the payee account.

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container_factory()
        logger.info("started")
        yield
        app.state.container.close()
        logger.info("shutdown complete")

    app = FastAPI(title="Merchant", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    install_common(app)
```

The lifespan: create the container at start, close it at stop (the HTTP client to the bank). `install_common` adds the shared logging and catch-all handlers.

```python
    def merchant(request: Request) -> MerchantContainer:
        result: MerchantContainer = request.app.state.container
        return result
```

A helper that fetches the container from the application's state.

```python
    @app.exception_handler(OpenBankingError)
    async def named_error(request: Request, error: OpenBankingError) -> JSONResponse:
        status = _STATUS.get(type(error), 500)
        return error_response(error.code, error.message, status, retryable=isinstance(error, BankUnavailableError))
```

Named errors are answered with their code, message and the status from the table; `retryable` is true only for `BankUnavailableError`, telling a caller it may complete the checkout again with the same consent.

```python
    @app.post("/v1/checkouts", status_code=201)
    def create_checkout(body: CheckoutBody, request: Request) -> JSONResponse:
        container = merchant(request)
        checkout = container.create_checkout.execute(Money(body.amount_minor, body.currency))
        return JSONResponse(checkout_view(checkout, container, payee_account), status_code=201)
```

`POST /v1/checkouts`: creates a checkout for the amount and answers 201 with its view.

```python
    @app.post("/v1/checkouts/{checkout_id}/complete")
    def complete_checkout(checkout_id: str, body: CompleteBody, request: Request) -> JSONResponse:
        container = merchant(request)
        checkout = container.complete_checkout.execute(checkout_id, body.consent_id)
        status = 200 if checkout.refusal is None else 403
        return JSONResponse(checkout_view(checkout, container, payee_account), status_code=status)
```

`POST /v1/checkouts/{id}/complete`: asks the bank to pay with the consent. The status is 200 when the checkout is not refused and 403 when the bank refused (the view carries the bank's reason).

```python
    @app.get("/v1/checkouts/{checkout_id}")
    def get_checkout(checkout_id: str, request: Request) -> JSONResponse:
        container = merchant(request)
        checkout = container.checkouts.get(checkout_id)
        if checkout is None:
            raise CheckoutNotFoundError(f"no checkout {checkout_id!r}")
        return JSONResponse(checkout_view(checkout, container, payee_account))
```

`GET /v1/checkouts/{id}`: reads one; an unknown id raises `CheckoutNotFoundError`.

```python
    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}
```

`/health/live` says the process is up.

```python
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}
```

`/health` also says `ok`: the merchant has no database to check.

```python
    return app
```

Returns the finished application.

### `src/open_banking/interfaces/serve_bank.py` (71 lines)

```python
"""`open-banking-bank`: run the bank HTTP API until SIGTERM or SIGINT, then finish in-flight requests and exit 0."""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Generator
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI

from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.logging import configure_logging
from open_banking.infrastructure.settings import ConfigurationError, ServerSettings
from open_banking.interfaces.bank_api import create_bank_app
from open_banking.interfaces.bank_container import build_bank_container
```

The launcher behind the command `open-banking-bank`: it runs the bank HTTP API until SIGTERM or SIGINT, then finishes in-flight requests and exits 0. The server launcher. `signal` lets a program catch operating-system signals; `threading` is used to check which thread runs; `uvicorn` is the web server; `FastAPI` is the application type.

```python
logger = logging.getLogger("open_banking.serve_bank")
```

A logger with a fixed name, `open_banking.serve_bank`.

```python
class GracefulServer(uvicorn.Server):
    """A uvicorn server that exits normally (status 0) after SIGINT or SIGTERM.

    uvicorn finishes in-flight requests and runs the lifespan shutdown on a signal, then raises the same signal again
    so the process dies "by signal". An orchestrator reads that as a crash; this server just returns.
    """
```

A subclass of uvicorn's server. The docstring explains the problem: on SIGINT or SIGTERM uvicorn finishes in-flight requests and runs the lifespan shutdown, and then raises the same signal again so the process dies "by signal". An orchestrator reads that as a crash. This class makes the program exit normally with status 0.

```python
    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        """Route SIGINT and SIGTERM to the server's own graceful exit, and put the old handlers back afterwards."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
```

`capture_signals` overrides uvicorn's method. It does nothing when not on the main thread (Python allows signal handlers only there). Otherwise it installs uvicorn's own handler (`self.handle_exit`) for SIGINT and SIGTERM with `signal.signal(sig, handler)`, which returns the old handler; the dictionary comprehension keeps the old handlers, and the `finally` block restores them. Unlike uvicorn's version it does not re-raise the signal afterwards.

```python
def serve(app: FastAPI, server: ServerSettings) -> None:
    """Serve `app` until told to stop."""
    config = uvicorn.Config(
        app,
        host=server.host,
        port=server.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=server.shutdown_grace_seconds,
    )
    GracefulServer(config).run()
```

`serve` builds the uvicorn configuration from the server settings: `log_config=None` leaves logging to this program, `access_log=False` because the middleware already logs requests, and `timeout_graceful_shutdown` is the wait for in-flight requests. `GracefulServer(config).run()` blocks until a stop signal.

```python
def main() -> int:
    """Start the server; the exit code is 0 after a graceful stop and 2 for a configuration error."""
    try:
        config = BankConfig.from_env()
    except ConfigurationError as error:
        print(f"configuration error: {error}")
        return 2
    configure_logging(config.log_level)
    logger.info("listening", extra={"fields": {"host": config.server.host, "port": config.server.port}})
    serve(create_bank_app(lambda: build_bank_container(config)), config.server)
    return 0
```

`main` returns the process exit status. It reads the bank's configuration (a bad one prints `configuration error: ...` and returns 2), configures logging, logs `listening` with the host and port, and serves the bank application, built by `create_bank_app` around a factory that builds the real container only when the app starts. After a graceful stop it returns 0.

```python
if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

The standard entry-point idiom: when the file is run as a program, exit with the status `main()` returns. `# pragma: no cover` excludes the line from coverage.

### `src/open_banking/interfaces/serve_merchant.py` (70 lines)

```python
"""`open-banking-merchant`: run the merchant HTTP API until SIGTERM or SIGINT, then exit 0."""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Generator
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI

from open_banking.infrastructure.logging import configure_logging
from open_banking.infrastructure.settings import ConfigurationError, MerchantConfig, ServerSettings
from open_banking.interfaces.merchant_api import create_merchant_app
from open_banking.interfaces.merchant_container import build_merchant_container
```

The launcher behind the command `open-banking-merchant`: it runs the merchant HTTP API until SIGTERM or SIGINT, then exits 0. The server launcher. `signal` lets a program catch operating-system signals; `threading` is used to check which thread runs; `uvicorn` is the web server; `FastAPI` is the application type.

```python
logger = logging.getLogger("open_banking.serve_merchant")
```

A logger with a fixed name, `open_banking.serve_merchant`.

```python
class GracefulServer(uvicorn.Server):
    """A uvicorn server that exits normally (status 0) after SIGINT or SIGTERM.

    uvicorn finishes in-flight requests and runs the lifespan shutdown on a signal, then raises the same signal again
    so the process dies "by signal". An orchestrator reads that as a crash; this server just returns.
    """
```

A subclass of uvicorn's server. The docstring explains the problem: on SIGINT or SIGTERM uvicorn finishes in-flight requests and runs the lifespan shutdown, and then raises the same signal again so the process dies "by signal". An orchestrator reads that as a crash. This class makes the program exit normally with status 0.

```python
    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        """Route SIGINT and SIGTERM to the server's own graceful exit, and put the old handlers back afterwards."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
```

`capture_signals` overrides uvicorn's method. It does nothing when not on the main thread (Python allows signal handlers only there). Otherwise it installs uvicorn's own handler (`self.handle_exit`) for SIGINT and SIGTERM with `signal.signal(sig, handler)`, which returns the old handler; the dictionary comprehension keeps the old handlers, and the `finally` block restores them. Unlike uvicorn's version it does not re-raise the signal afterwards.

```python
def serve(app: FastAPI, server: ServerSettings) -> None:
    """Serve `app` until told to stop."""
    config = uvicorn.Config(
        app,
        host=server.host,
        port=server.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=server.shutdown_grace_seconds,
    )
    GracefulServer(config).run()
```

`serve` builds the uvicorn configuration from the server settings: `log_config=None` leaves logging to this program, `access_log=False` because the middleware already logs requests, and `timeout_graceful_shutdown` is the wait for in-flight requests. `GracefulServer(config).run()` blocks until a stop signal.

```python
def main() -> int:
    """Start the server; the exit code is 0 after a graceful stop and 2 for a configuration error."""
    try:
        config = MerchantConfig.from_env()
    except ConfigurationError as error:
        print(f"configuration error: {error}")
        return 2
    configure_logging(config.log_level)
    logger.info("listening", extra={"fields": {"host": config.server.host, "port": config.server.port}})
    serve(create_merchant_app(lambda: build_merchant_container(config), config.payee_account), config.server)
    return 0
```

`main` reads the merchant's configuration (a bad one prints `configuration error: ...` and returns 2), configures logging, logs `listening`, and serves the merchant application built around a factory that creates the merchant container (the HTTP client to the bank, which reads the merchant's private key file) and the payee account. After a graceful stop it returns 0.

```python
if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

Exits with the status `main()` returns when the file is run as a program.

### `src/open_banking/interfaces/health_cli.py` (24 lines)

```python
"""`open-banking-health`: exit 0 when the bank (or, with HEALTH_URL, any service) reports healthy."""

from __future__ import annotations

import os
import sys

import httpx
```

The command `open-banking-health`: it exits 0 when the bank (or, with `HEALTH_URL`, any service) reports healthy. It is used as a health check by scripts and the container. `os` reads environment variables; `sys` provides `exit`; `httpx` is the HTTP client.

```python
def main() -> int:
    """Ask `/health` of HEALTH_URL (default: the bank at BANK_HOST:BANK_PORT); exit 0 for healthy, 1 otherwise."""
    default = f"http://{os.environ.get('BANK_HOST', '127.0.0.1')}:{os.environ.get('BANK_PORT', '8463')}/health"
    try:
        response = httpx.get(os.environ.get("HEALTH_URL", default), timeout=5.0)
    except httpx.HTTPError as error:
        print(f"unreachable: {type(error).__name__}")
        return 1
    print(response.text)
    return 0 if response.status_code == 200 else 1
```

`default` is the address of the bank's `/health`, built from `BANK_HOST` and `BANK_PORT` (defaults `127.0.0.1` and `8463`; `os.environ.get(name, default)` reads a variable or uses the default). `HEALTH_URL`, if set, replaces it, so the same command can check the merchant. `httpx.get(..., timeout=5.0)` asks; `httpx.HTTPError` (cannot connect, timeout, or any other transport problem) prints `unreachable: <ErrorName>` and returns 1. Otherwise the answer text is printed and the exit status is 0 for status 200 and 1 for any other.

```python
if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
```

`sys.exit(main())` turns the returned number into the process exit status.

### `src/open_banking/interfaces/admin_cli.py` (102 lines)

```python
"""`open-banking-admin`: the bank operator's program. Register a merchant, enroll a customer, read balances.

Secrets it creates (a merchant's key and client secret, a customer's PIN) are written to files readable by their owner
only, under `--out` (default `var/`, which git ignores). They are never printed, and the bank keeps only hashes.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

from open_banking.domain.errors import OpenBankingError
from open_banking.infrastructure import keys
from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.logging import configure_logging
from open_banking.infrastructure.settings import ConfigurationError
from open_banking.interfaces.bank_container import BankContainer, build_bank_container
```

`open-banking-admin`: the bank operator's program. Register a merchant, enroll a customer, read balances. The docstring says that the secrets it creates (a merchant's key and client secret, a customer's PIN) are written to files readable by their owner only, under `--out` (default `var/`, which git ignores); they are never printed, and the bank keeps only hashes. `argparse` is the standard library's command-line parser; `secrets` draws secure random values.

```python
def _write_secret_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(text, encoding="utf-8")
```

Writes a file only its owner can read: it creates the folder, creates the file with permission `0o600`, sets the permission again in case the file existed, then writes the text.

```python
def register_merchant(bank: BankContainer, client_id: str, name: str, out: Path) -> list[Path]:
    """Register a merchant and write its private key and credentials file. Returns the files written."""
    private_pem, public_jwk = keys.generate_keypair()
    client_secret = secrets.token_urlsafe(32)
    bank.register_client.execute(client_id, name, client_secret, public_jwk)
    key_file = out / f"{client_id}-key.pem"
    env_file = out / f"{client_id}.env"
    keys.write_private_key(key_file, private_pem)
    _write_secret_file(
        env_file,
        f"MERCHANT_CLIENT_ID={client_id}\nMERCHANT_CLIENT_SECRET={client_secret}\n"
        f"MERCHANT_PRIVATE_KEY_FILE={key_file}\n",
    )
    return [key_file, env_file]
```

Registers a merchant. It generates a key pair, draws a random client secret (`secrets.token_urlsafe(32)`), registers the client at the bank with the secret and the **public** key, writes the private key to `<out>/<client id>-key.pem` and an `.env` file with the client id, the secret and the key file path. The bank gets only the hash of the secret and the public key. It returns the two files written.

```python
def enroll_customer(bank: BankContainer, username: str, account_id: str, out: Path) -> Path:
    """Enroll a customer with a random six-digit PIN, written to a file. Returns the file."""
    pin = f"{secrets.randbelow(10**6):06d}"
    bank.enroll_customer.execute(username, pin, account_id)
    pin_file = out / f"{username}.pin"
    _write_secret_file(pin_file, pin + "\n")
    return pin_file
```

Enrolls a customer with a random six-digit PIN (`secrets.randbelow(10**6)` with leading zeros, `:06d`), stored by the bank only as a hash, and writes the PIN to `<out>/<username>.pin`. It returns that file.

```python
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="open-banking-admin", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    merchant = commands.add_parser("register-merchant", help="register a merchant and create its key and secret")
    merchant.add_argument("--client-id", required=True)
    merchant.add_argument("--name", required=True)
    merchant.add_argument("--out", type=Path, default=Path("var"))
    customer = commands.add_parser("enroll-customer", help="enroll a customer with a random PIN")
    customer.add_argument("--username", required=True)
    customer.add_argument("--account", required=True)
    customer.add_argument("--out", type=Path, default=Path("var"))
    balances = commands.add_parser("balances", help="print account balances and the ledger sum")
    balances.add_argument("accounts", nargs="+")
    return parser
```

Builds the command-line parser with three sub-commands: `register-merchant` (`--client-id`, `--name`, `--out`), `enroll-customer` (`--username`, `--account`, `--out`) and `balances` (one or more account ids, `nargs="+"`). `required=True` on an option makes it mandatory; `type=Path` converts the text to a path.

```python
def main(argv: Sequence[str] | None = None) -> int:
    """Run a command; exit 0 on success, 1 for a refused operation, 2 for configuration."""
    args = _parser().parse_args(argv)
    try:
        config = BankConfig.from_env()
    except ConfigurationError as error:
        print(f"configuration error: {error}")
        return 2
    configure_logging("WARNING")
    bank = build_bank_container(config)
    try:
        if args.command == "register-merchant":
            files = register_merchant(bank, args.client_id, args.name, args.out)
            print(f"registered {args.client_id}; wrote " + ", ".join(str(path) for path in files))
        elif args.command == "enroll-customer":
            pin_file = enroll_customer(bank, args.username, args.account, args.out)
            print(f"enrolled {args.username}; the PIN is in {pin_file}")
        else:
            balances, total = bank.balances.execute(args.accounts)
            for account, balance in balances.items():
                print(f"{account}: {balance.format()} {balance.currency}")
            print(f"ledger sum across every posting: {total.format()}")
        return 0
    except OpenBankingError as error:
        print(f"refused: {error.message}")
        return 1
    finally:
        bank.close()
```

Runs a command. Exit codes: 0 success, 1 a refused operation, 2 a configuration error. It builds the bank container, runs the chosen command (printing the files written, or the balances and the ledger sum) and always closes the container in `finally`. A secret is never printed: the output names the files only.

```python
if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
```

Exits with the status `main()` returns.

### `src/open_banking/interfaces/customer_cli.py` (136 lines)

```python
"""`open-banking-customer`: the customer's side. It talks to the bank directly; the merchant is never in the path.

`approve` authenticates (PIN, then the one-time code) and creates a consent; it prints the consent id, which the
customer takes back to the merchant. `revoke` withdraws a consent.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from open_banking.infrastructure.otp import FileOtpChannel
```

`open-banking-customer`: the customer's side. It talks to the bank directly; the merchant is never in the path. `approve` authenticates (PIN, then the one-time code) and creates a consent, printing the consent id the customer takes back to the merchant; `revoke` withdraws a consent. New imports: `getpass` (reads a password without echoing it), `Decimal` (exact decimal numbers) and `InvalidOperation` (its error).

```python
class CustomerError(Exception):
    """The bank said no, or the input was wrong."""
```

Raised when the bank said no or the input was wrong.

```python
def _terms(args: argparse.Namespace, payer_account: str) -> dict[str, Any]:
    try:
        minor = int(Decimal(args.max) * 100)
    except InvalidOperation as error:
        raise CustomerError(f"--max must be an amount such as 50.00, got {args.max!r}") from error
    return {
        "payer_account": payer_account,
        "payee_account": args.payee,
        "client_id": args.merchant,
        "max_amount_minor": minor,
        "currency": args.currency,
        "scope": args.scope,
        "period_seconds": None if args.scope == "single-payment" else int(args.period_days) * 86400,
    }
```

Builds the terms dictionary for the bank. `int(Decimal(args.max) * 100)` converts an amount such as `50.00` to 5000 minor units exactly (a `Decimal` has no binary rounding); a text that is not a number raises `InvalidOperation`, which becomes a `CustomerError` with a helpful message. A single-payment consent has no period; a recurring one has `period-days * 86400` seconds.

```python
def _check(response: httpx.Response) -> dict[str, Any]:
    payload: dict[str, Any] = response.json()
    if response.status_code >= 400:
        error = payload.get("error", {})
        raise CustomerError(f"{error.get('code', response.status_code)}: {error.get('message', response.text)}")
    return payload
```

Reads the JSON answer; a status of 400 or more raises `CustomerError` with the bank's error code and message.

```python
def approve(args: argparse.Namespace, pin: str, read_code: Any) -> str | None:
    """Start the challenge, enter the code, create the consent. Returns the consent id, or None when abandoned."""
    with httpx.Client(base_url=args.bank_url, timeout=15.0) as bank:
        challenge = _check(
            bank.post(
                "/v1/sca/challenges",
                json={"username": args.username, "pin": pin, "terms": _terms(args, args.account)},
            )
        )
        print(f"a code was sent to your device; challenge {challenge['challenge_id']}")
        if args.abandon:
            print("abandoned: no code entered, no consent created")
            return None
        proof = _check(
            bank.post(f"/v1/sca/challenges/{challenge['challenge_id']}/verify", json={"code": read_code(challenge)})
        )
        consent = _check(
            bank.post(
                "/v1/consents",
                json={"terms": _terms(args, args.account)},
                headers={"Authorization": f"SCA {proof['authentication_proof']}"},
            )
        )
        print(json.dumps({"consent_id": consent["consent_id"], "expires_at": consent["expires_at"]}))
        return str(consent["consent_id"])
```

The whole approval. In one HTTP client for the bank: (1) post the challenge with the username, PIN and terms and print that a code was sent; (2) with `--abandon`, stop here (no consent is created); (3) post the code (obtained through `read_code`) to the verify endpoint and take the proof; (4) post the consent with the header `Authorization: SCA <proof>`; (5) print the consent id and expiry as JSON and return the id.

```python
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="open-banking-customer", description=__doc__)
    parser.add_argument("--bank-url", default=os.environ.get("BANK_URL", "http://127.0.0.1:8463"))
    commands = parser.add_subparsers(dest="command", required=True)
    approve_parser = commands.add_parser("approve", help="authenticate and create a consent")
    approve_parser.add_argument("--username", required=True)
    approve_parser.add_argument("--account", required=True, help="the payer account (yours)")
    approve_parser.add_argument("--payee", required=True, help="the account the merchant is paid into")
    approve_parser.add_argument("--merchant", required=True, help="the merchant's client id")
    approve_parser.add_argument("--max", required=True, help="the most the merchant may take, such as 50.00")
    approve_parser.add_argument("--currency", default="USD")
    approve_parser.add_argument("--scope", choices=["single-payment", "recurring"], default="single-payment")
    approve_parser.add_argument("--period-days", default="30", help="the period of a recurring consent's cap")
    approve_parser.add_argument("--pin-file", type=Path, help="read the PIN from this file instead of asking")
    approve_parser.add_argument("--code", help="the one-time code (otherwise read from --outbox, or asked)")
    approve_parser.add_argument("--outbox", type=Path, help="read the code from the stand-in phone file")
    approve_parser.add_argument("--abandon", action="store_true", help="stop after the code is sent")
    revoke_parser = commands.add_parser("revoke", help="withdraw a consent")
    revoke_parser.add_argument("--username", required=True)
    revoke_parser.add_argument("--consent-id", required=True)
    revoke_parser.add_argument("--pin-file", type=Path)
    return parser
```

Builds the parser. A global `--bank-url` (default from the environment variable `BANK_URL`). `approve` takes the username, the payer account, the payee account, the merchant id, the maximum, the currency, the scope (`choices=[...]` restricts the values), the period in days, and where the PIN and the code come from: `--pin-file`, `--code`, `--outbox` (the stand-in phone file); `--abandon` (`action="store_true"`) stops after the code is sent. `revoke` takes the username, the consent id and the PIN file.

```python
def _read_pin(args: argparse.Namespace) -> str:
    if args.pin_file:
        return str(args.pin_file.read_text(encoding="utf-8").strip())
    return os.environ.get("CUSTOMER_PIN") or getpass.getpass("PIN: ")
```

Where the PIN comes from, in order: a file, the environment variable `CUSTOMER_PIN`, or an interactive prompt with `getpass.getpass`, which does not echo what is typed.

```python
def main(argv: Sequence[str] | None = None) -> int:
    """Run a command; exit 0 on success, 1 when the bank refused."""
    args = _parser().parse_args(argv)
    try:
        pin = _read_pin(args)
        if args.command == "revoke":
            with httpx.Client(base_url=args.bank_url, timeout=15.0) as bank:
                _check(
                    bank.post(f"/v1/consents/{args.consent_id}/revoke", json={"username": args.username, "pin": pin})
                )
            print(f"consent {args.consent_id} revoked")
            return 0

        def read_code(challenge: dict[str, Any]) -> str:
            if args.code:
                return str(args.code)
            latest = FileOtpChannel(args.outbox).latest_any() if args.outbox else None
            return latest or input("Code from your device: ").strip()

        approve(args, pin, read_code)
        return 0
    except (CustomerError, httpx.HTTPError, OSError) as error:
        print(f"refused: {error}")
        return 1
```

Runs a command. For `revoke` it posts the username and PIN to the revoke endpoint and prints a confirmation. For `approve` the nested function `read_code` finds the code: the `--code` option, else the newest code in the outbox file, else it asks the user to type it (`input`). Errors of the bank, HTTP problems and file problems print `refused: ...` and return 1.

```python
if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
```

Exits with the status `main()` returns.

### `src/open_banking/interfaces/demo_cli.py` (646 lines)

```python
"""`open-banking-demo`: the lesson's scenario with a bank and a merchant server running in this process.

Customer actions go straight to the bank over HTTP (the merchant is never in that path). Each section prints what the
lesson's "Expected output" shows and the program exits non-zero if the system does not behave.
"""

from __future__ import annotations

import argparse
import io
import socket
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from open_banking.application.merchant.checkout import BankUnavailableError
from open_banking.domain.money import Money
from open_banking.domain.signing import canonical_payment_request
from open_banking.infrastructure import keys
from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.faults import Fault
from open_banking.infrastructure.logging import configure_logging
from open_banking.infrastructure.otp import FileOtpChannel
from open_banking.infrastructure.settings import ConfigurationError, MerchantConfig, ServerSettings
from open_banking.infrastructure.system import OffsetClock
from open_banking.interfaces.admin_cli import register_merchant
from open_banking.interfaces.bank_api import create_bank_app
from open_banking.interfaces.bank_container import BankContainer, build_bank_container
from open_banking.interfaces.merchant_api import create_merchant_app
from open_banking.interfaces.merchant_container import MerchantContainer, build_merchant_container
```

`open-banking-demo`: the lesson's scenario with a bank and a merchant server running in this process. The docstring: customer actions go straight to the bank over HTTP (the merchant is never in that path); each section prints what the lesson's "Expected output" shows, and the program exits non-zero if the system does not behave. New imports: `socket`, `uuid`, `ThreadPoolExecutor`, `uvicorn` and many of the project's own pieces.

```python
class ScenarioError(AssertionError):
    """The system did something the lesson says it must not."""
```

Raised when the system did something the lesson says it must not.

```python
def expect(condition: bool, message: str) -> None:
    """Raise `ScenarioError` with `message` unless `condition` holds."""
    if not condition:
        raise ScenarioError(message)
```

Raises `ScenarioError(message)` unless the condition holds: the demo's own assertion, which unlike `assert` is never removed by Python's optimization flag.

```python
def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
```

Finds an unused port: bind to port 0 (the operating system picks a free one), read it back with `getsockname()` and close.

```python
def _serve_in_thread(app: Any, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise ScenarioError("a demo server did not start")
        time.sleep(0.05)
    return server, thread
```

Starts a uvicorn server in a background thread (`daemon=True` so it cannot keep the program alive) and waits up to 30 seconds (`time.monotonic()` never jumps) until `server.started`; if the thread dies or the time passes it raises `ScenarioError`. It returns the server and the thread.

```python
@dataclass
class Person:
    """A customer's credentials, as the demo holds them (read from the files the admin step wrote)."""

    username: str
    account: str
    pin: str
```

A customer's credentials as the demo holds them: username, account and PIN.

```python
@dataclass
class Stack:
    """Everything the scenario talks to."""

    bank: BankContainer
    merchant: MerchantContainer
    other_merchant: MerchantContainer
    clock: OffsetClock
    bank_url: str
    merchant_url: str
    phone: FileOtpChannel
    ada: Person
    bob: Person
    merchant_key: str
    other_key: str
    merchant_credentials: tuple[str, str]
    http: httpx.Client
```

Everything the scenario talks to: the bank container, two merchant containers, the offset clock, the two URLs, the stand-in phone, the two customers (`ada` and `bob`), the merchants' private keys, the first merchant's credentials and an HTTP client.

```python
def _merchant_config(bank_url: str, port: int, client_id: str, payee: str, out: Path) -> MerchantConfig:
    values = dict(
        line.split("=", 1)
        for line in (out / f"{client_id}.env").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return MerchantConfig(
        server=ServerSettings("127.0.0.1", port, 10),
        bank_url=bank_url,
        client_id=client_id,
        client_secret=values["MERCHANT_CLIENT_SECRET"],
        private_key_file=Path(values["MERCHANT_PRIVATE_KEY_FILE"]),
        payee_account=payee,
        request_timeout_seconds=10.0,
        attempts=3,
        retry_base_delay_ms=20,
        log_level="WARNING",
    )
```

Builds a `MerchantConfig` for a merchant from the `.env` file the admin step wrote: the lines are split at the first `=` into a dictionary (`dict(line.split("=", 1) for line in ... if "=" in line)`); the secret and the key file path are taken from it, the rest are fixed demo values (three attempts, 20 ms base delay).

```python
@contextmanager
def running_stack(out: Path) -> Iterator[Stack]:
    """Start the bank and the merchant, register two merchants, enroll two customers; stop everything afterwards."""
    base = BankConfig.from_env()
    bank_port, merchant_port = _free_port(), _free_port()
    bank_url = f"http://127.0.0.1:{bank_port}"
    config = BankConfig(
        postgres=base.postgres,
        server=ServerSettings("127.0.0.1", bank_port, 10),
        public_url=bank_url,
        timings=base.timings,
        pepper=base.pepper,
        signing_key_file=out / "bank-signing-key.pem",
        otp_outbox_file=out / "otp-outbox.log",
        scrypt_n=base.scrypt_n,
        log_level="WARNING",
    )
    clock = OffsetClock()
    bank = build_bank_container(config, clock=clock)
    people: dict[str, Person] = {}
    for username, account in (("ada", "acct-ada"), ("bob", "acct-bob")):
        pin = f"{uuid.uuid4().int % 10**6:06d}"
        bank.enroll_customer.execute(username, pin, account)
        people[username] = Person(username, account, pin)
    register_merchant(bank, "merchant-1", "Shop", out)
    register_merchant(bank, "merchant-2", "Other Shop", out)
    shop = _merchant_config(bank_url, merchant_port, "merchant-1", "acct-shop", out)
    other = _merchant_config(bank_url, _free_port(), "merchant-2", "acct-other-shop", out)
    merchant = build_merchant_container(shop, clock=clock.now, bank_public_jwk=bank.signing_jwk)
    other_merchant = build_merchant_container(other, clock=clock.now, bank_public_jwk=bank.signing_jwk)
    bank_server, bank_thread = _serve_in_thread(create_bank_app(lambda: bank), bank_port)
    merchant_server, merchant_thread = _serve_in_thread(
        create_merchant_app(lambda: merchant, shop.payee_account), merchant_port
    )
    http = httpx.Client(timeout=30.0)
    try:
        yield Stack(
            bank=bank,
            merchant=merchant,
            other_merchant=other_merchant,
            clock=clock,
            bank_url=bank_url,
            merchant_url=f"http://127.0.0.1:{merchant_port}",
            phone=FileOtpChannel(config.otp_outbox_file),
            ada=people["ada"],
            bob=people["bob"],
            merchant_key=shop.private_key_file.read_text(encoding="utf-8"),
            other_key=other.private_key_file.read_text(encoding="utf-8"),
            merchant_credentials=(shop.client_id, shop.client_secret),
            http=http,
        )
    finally:
        http.close()
        for server, thread in ((merchant_server, merchant_thread), (bank_server, bank_thread)):
            server.should_exit = True
            thread.join(timeout=15)
        merchant.close()
        other_merchant.close()
        bank.close()
```

`running_stack` is a context manager that builds the whole world and tears it down. Inside: it reads the bank configuration from the environment and picks two free ports; makes a configuration that uses them and a demo folder for the signing key and the phone file; builds the bank with an `OffsetClock` (so the demo can move time); enrolls `ada` and `bob` with random PINs; registers two merchants through the admin function; builds the two merchant containers (one for the shop, one "other shop") with the bank's public signing key; starts the bank and the shop's merchant API in background threads; and yields a `Stack`. The `finally` block closes the client, asks both servers to stop (`should_exit = True`), waits for the threads, and closes the containers.

```python
def _terms(person: Person, payee: str, merchant: str, max_minor: int, **extra: Any) -> dict[str, Any]:
    return {
        "payer_account": person.account,
        "payee_account": payee,
        "client_id": merchant,
        "max_amount_minor": max_minor,
        "currency": "USD",
        "scope": "single-payment",
        "period_seconds": None,
        **extra,
    }
```

Builds the terms dictionary for a customer: payer account, payee, merchant, maximum, currency, scope and period, with `**extra` overriding any field (for a recurring consent).

```python
def authenticate(stack: Stack, person: Person, terms: dict[str, Any]) -> str:
    """The customer, at the bank only: PIN, then the code from the phone. Returns the proof of authentication."""
    started = stack.http.post(
        f"{stack.bank_url}/v1/sca/challenges", json={"username": person.username, "pin": person.pin, "terms": terms}
    )
    expect(started.status_code == 201, f"challenge refused: {started.text}")
    code = stack.phone.latest_any()
    expect(code is not None, "no code reached the phone")
    verified = stack.http.post(
        f"{stack.bank_url}/v1/sca/challenges/{started.json()['challenge_id']}/verify", json={"code": code}
    )
    expect(verified.status_code == 200, f"code refused: {verified.text}")
    return str(verified.json()["authentication_proof"])
```

The customer at the bank only: post the challenge with the PIN (expect 201), read the code from the stand-in phone, post the code (expect 200) and return the authentication proof.

```python
def grant(stack: Stack, person: Person, terms: dict[str, Any]) -> dict[str, Any]:
    """Authenticate and create a consent. Returns the consent."""
    proof = authenticate(stack, person, terms)
    response = stack.http.post(
        f"{stack.bank_url}/v1/consents", json={"terms": terms}, headers={"Authorization": f"SCA {proof}"}
    )
    expect(response.status_code == 201, f"consent refused: {response.text}")
    consent: dict[str, Any] = response.json()
    return consent
```

Authenticates and creates a consent with the proof in the `SCA` header; returns the consent.

```python
def raw_payment(
    stack: Stack,
    *,
    consent_id: str,
    amount_minor: int,
    key: str,
    payee: str = "acct-shop",
    signed_minor: int | None = None,
    signing_key: str | None = None,
    token_holder_key: str | None = None,
    token: str | None = None,
    client_id: str = "merchant-1",
    client_secret: str | None = None,
    send_credential: bool = True,
) -> httpx.Response:
    """A payment request built by hand, so the scenario can break one thing at a time."""
    private = signing_key or stack.merchant_key
    public = keys.public_jwk_of(private)
    url = f"{stack.bank_url}/v1/payments"
    headers: dict[str, str] = {"Idempotency-Key": key}
    if send_credential:
        access = token or _token(stack, client_id, client_secret or stack.merchant_credentials[1], private)
        holder = token_holder_key or private
        headers["Authorization"] = f"DPoP {access}"
        headers["DPoP"] = keys.build_dpop_proof(
            holder, keys.public_jwk_of(holder), method="POST", url=url, now=stack.clock.now(), access_token=access
        )
    signed = Money(signed_minor if signed_minor is not None else amount_minor, "USD")
    headers["X-Request-Signature"] = keys.sign(
        private,
        canonical_payment_request(
            client_id=client_id, consent_id=consent_id, payee_account=payee, amount=signed, idempotency_key=key
        ),
    )
    _ = public
    body = {"consent_id": consent_id, "payee_account": payee, "amount_minor": amount_minor, "currency": "USD"}
    return stack.http.post(url, json=body, headers=headers)
```

Builds a payment request by hand so the scenario can break **one thing at a time**. The keyword arguments can change the amount that is sent, the amount that is signed, the payee, the signing key, the key the token is bound to, a ready-made token, the client id and secret, or leave out the credentials. If credentials are sent, it gets a token (or uses the given one) and makes a DPoP proof with the "holder" key; it then signs the canonical request (with `signed_minor` if different from the amount, to imitate tampering) and posts the JSON body with the headers.

```python
def token_request(stack: Stack, client_id: str, secret: str, private: str) -> httpx.Response:
    """Ask the bank for a token with a client secret and a proof made by `private`."""
    url = f"{stack.bank_url}/oauth/token"
    proof = keys.build_dpop_proof(private, keys.public_jwk_of(private), method="POST", url=url, now=stack.clock.now())
    return stack.http.post(
        url, data={"grant_type": "client_credentials"}, auth=(client_id, secret), headers={"DPoP": proof}
    )
```

Asks the bank for a token with a client secret and a proof made by the given private key.

```python
def _token(stack: Stack, client_id: str, secret: str, private: str) -> str:
    response = token_request(stack, client_id, secret, private)
    expect(response.status_code == 200, f"token refused: {response.text}")
    return str(response.json()["access_token"])
```

Requests a token and requires status 200; returns the token.

```python
def balances(stack: Stack) -> dict[str, int]:
    """The balances the scenario watches, in minor units."""
    found, _ = stack.bank.balances.execute(["acct-ada", "acct-bob", "acct-shop", "acct-other-shop"])
    return {account: money.minor for account, money in found.items()}
```

The balances the scenario watches (four accounts) in minor units.

```python
def ledger_total(stack: Stack) -> int:
    """The sum of every posting at the bank."""
    return stack.bank.balances.execute(["acct-ada"])[1].minor
```

The sum of every posting at the bank, as minor units.

```python
def error_line(response: httpx.Response) -> str:
    """`status code: message` of an error response."""
    error = response.json()["error"]
    return f"{response.status_code} {error['code']}: {error['message']}"
```

Formats an error answer as `status code: message`.

```python
def section_trust(stack: Stack) -> None:
    """Step 1: where the trust lies, and what the merchant can reach."""
    print("== 1. The parties and the trust")
    print("customer <-> bank:     strong customer authentication (PIN, then a code on the customer's device), direct")
    print("merchant <-> bank:     client secret + a token bound to the merchant's own key, a fresh proof per request")
    print("customer <-> merchant: the customer trusts the merchant to ask for what was agreed; the bank checks it")
    print("the merchant's client calls only: POST /oauth/token and POST /v1/payments")
    print("the merchant process imports none of the bank's authentication code (enforced by `make lint`)")
    print(
        "the customer's PIN and the code never leave customer and bank; "
        f"the merchant holds {stack.merchant_credentials[0]}'s key and nothing else"
    )
    print()
```

Step 1 prints where the trust lies: the customer authenticates directly with the bank (PIN, then a code); the merchant authenticates with a client secret and a token bound to its own key, with a fresh proof per request; the merchant's client calls only the token and payment endpoints, and imports none of the bank's authentication code (a lint rule enforces that).

```python
def section_consent(stack: Stack) -> dict[str, Any]:
    """Step 2: consent exists only after strong authentication."""
    print("== 2. Consent is an object, and it exists only after strong authentication")
    terms = _terms(stack.ada, "acct-shop", "merchant-1", 5000)
    response = stack.http.post(f"{stack.bank_url}/v1/consents", json={"terms": terms})
    print(f"consent without authentication -> {error_line(response)}")
    expect(response.status_code == 401, "a consent without a proof must be refused")
    response = stack.http.post(
        f"{stack.bank_url}/v1/sca/challenges", json={"username": "ada", "pin": "000000", "terms": terms}
    )
    print(f"wrong PIN -> {error_line(response)}")
    expect(response.status_code == 401, "a wrong PIN must be refused")
    count = _consent_count(stack)
    started = stack.http.post(
        f"{stack.bank_url}/v1/sca/challenges", json={"username": "ada", "pin": stack.ada.pin, "terms": terms}
    )
    print(f"abandoned authentication: the code was sent ({started.json()['challenge_id'][:8]}...) and never entered")
    expect(_consent_count(stack) == count, "an abandoned authentication must not create a consent")
    print(f"consents before and after: {count} and {_consent_count(stack)}")
    locked = [
        stack.http.post(
            f"{stack.bank_url}/v1/sca/challenges/{started.json()['challenge_id']}/verify", json={"code": "000000"}
        )
        for _ in range(3)
    ]
    print("three wrong codes -> " + ", ".join(response.json()["error"]["code"] for response in locked))
    expect(locked[-1].json()["error"]["code"] == "challenge_locked", "three wrong codes must lock the challenge")
    proof = authenticate(stack, stack.ada, terms)
    bigger = _terms(stack.ada, "acct-shop", "merchant-1", 500_000)
    response = stack.http.post(
        f"{stack.bank_url}/v1/consents", json={"terms": bigger}, headers={"Authorization": f"SCA {proof}"}
    )
    print(f"a proof for 50.00 used for a consent of 5,000.00 -> {error_line(response)}")
    expect(response.status_code == 401, "a proof must be tied to the terms the customer saw")
    consent = stack.http.post(
        f"{stack.bank_url}/v1/consents", json={"terms": terms}, headers={"Authorization": f"SCA {proof}"}
    ).json()
    print(
        f"consent_id={consent['consent_id']}, scope={consent['scope']}, "
        f"max={Money(consent['max_amount_minor'], 'USD').format()}, "
        f"expires_at={consent['expires_at']} (10 minutes from now)"
    )
    again = stack.http.post(
        f"{stack.bank_url}/v1/consents", json={"terms": terms}, headers={"Authorization": f"SCA {proof}"}
    )
    print(f"the same proof used again -> {error_line(again)}")
    expect(again.status_code == 401, "a proof is good for one consent")
    print()
    return dict(consent)
```

Step 2 shows that consent exists only after strong authentication. It tries, and requires the right refusal each time: a consent with no proof (401); a challenge with a wrong PIN (401); an abandoned authentication (a code is sent and never entered, and the number of consents does not change); three wrong codes (the challenge locks: `challenge_locked`); a proof for 50.00 used for a consent of 5,000.00 (401, because the proof is tied to the terms); and the same proof used twice (the second use is refused). It finally creates the real consent and returns it.

```python
def _consent_count(stack: Stack) -> int:
    return stack.bank.balances.counts().consents
```

How many consents the bank holds.

```python
def section_merchant_access(stack: Stack, consent: dict[str, Any]) -> None:
    """Step 3: the merchant-to-bank API."""
    print("== 3. The merchant-to-bank API refuses what it should")
    key = f"s3-{uuid.uuid4().hex[:8]}"
    response = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=3000, key=key, send_credential=False)
    print(f"no merchant credential -> {error_line(response)}")
    expect(response.status_code == 401, "a request with no credential must be refused")
    token = _token(stack, "merchant-1", stack.merchant_credentials[1], stack.merchant_key)
    response = raw_payment(
        stack,
        consent_id=consent["consent_id"],
        amount_minor=3000,
        key=key,
        token=token,
        token_holder_key=stack.other_key,
    )
    print(f"a stolen token used with another key -> {error_line(response)}")
    expect(response.status_code == 401, "a token must only work with the key it is bound to")
    response = token_request(stack, "merchant-1", "wrong-secret-value", stack.merchant_key)
    print(f"a wrong client secret at the token endpoint -> {error_line(response)}")
    expect(response.status_code == 401, "a wrong secret must be refused")
    token_url = f"{stack.bank_url}/oauth/token"
    proof = keys.build_dpop_proof(
        stack.merchant_key, keys.public_jwk_of(stack.merchant_key), method="POST", url=token_url, now=stack.clock.now()
    )
    form = {"grant_type": "client_credentials"}
    first = stack.http.post(token_url, data=form, auth=stack.merchant_credentials, headers={"DPoP": proof})
    replay = stack.http.post(token_url, data=form, auth=stack.merchant_credentials, headers={"DPoP": proof})
    print(f"a proof of possession used twice -> {first.status_code}, then {error_line(replay)}")
    expect(first.status_code == 200 and replay.status_code == 401, "a captured proof must not be replayable")
    other = stack.other_merchant.complete_checkout
    checkout = stack.other_merchant.create_checkout.execute(Money(3000, "USD"))
    refused = other.execute(checkout.checkout_id, consent["consent_id"])
    print(
        "another merchant presents this consent -> "
        f"{refused.refusal.code if refused.refusal else 'paid'}: {refused.refusal.message if refused.refusal else ''}"
    )
    expect(
        refused.refusal is not None and refused.refusal.code == "consent_not_granted_to_caller",
        "another merchant's consent",
    )
    print()
```

Step 3 shows the merchant-to-bank API refusing what it should: no credential (401); a stolen token used with another merchant's key (401, because the token is bound to a key); a wrong client secret (401); a proof of possession used twice (the first request gets a token, the replay is refused); and another merchant presenting this consent (refused as `consent_not_granted_to_caller`).

```python
def section_within_consent(stack: Stack) -> dict[str, Any]:
    """Step 4: each rule of the consent, one refusal at a time, then a payment through the merchant."""
    print("== 4. A payment within consent succeeds; over the amount, another payee and expiry are each refused")
    consent = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    for label, kwargs in (
        ("amount=60.00", {"amount_minor": 6000}),
        ("payee_account=acct-other-shop", {"amount_minor": 3000, "payee": "acct-other-shop"}),
    ):
        response = raw_payment(stack, consent_id=consent["consent_id"], key=f"s4-{uuid.uuid4().hex[:8]}", **kwargs)
        print(f"{label:<32}-> {error_line(response)}")
        expect(response.status_code == 403, f"{label} must be refused")
    before = balances(stack)
    created = stack.http.post(f"{stack.merchant_url}/v1/checkouts", json={"amount_minor": 3000, "currency": "USD"})
    checkout = created.json()
    done = stack.http.post(
        f"{stack.merchant_url}/v1/checkouts/{checkout['checkout_id']}/complete",
        json={"consent_id": consent["consent_id"]},
    ).json()
    after = balances(stack)
    print(f"amount=30.00, correct payee     -> Success, payment {done['payment_id']}, status {done['status']}")
    print(
        f"ada {Money(before['acct-ada'], 'USD').format()} -> {Money(after['acct-ada'], 'USD').format()}; "
        f"shop {Money(before['acct-shop'], 'USD').format()} -> {Money(after['acct-shop'], 'USD').format()}; "
        f"ledger sum {Money(ledger_total(stack), 'USD').format()}"
    )
    expect(done["status"] == "PAID", "a payment within the consent must succeed")
    expect(
        before["acct-ada"] - after["acct-ada"] == 3000 and after["acct-shop"] - before["acct-shop"] == 3000,
        "30.00 moved",
    )
    expect(ledger_total(stack) == 0, "the ledger must sum to zero")
    expired = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    stack.clock.advance(timedelta(minutes=10, seconds=1))
    response = raw_payment(stack, consent_id=expired["consent_id"], amount_minor=3000, key=f"s4-{uuid.uuid4().hex[:8]}")
    print(f"clock 1 second past expires_at  -> {error_line(response)}")
    expect(response.status_code == 403 and response.json()["error"]["code"] == "consent_expired", "an expired consent")
    print()
    return dict(consent)
```

Step 4: each rule of the consent, one refusal at a time, then a payment through the merchant. Over the amount (60.00 against 50.00) and another payee are each refused with status 403. Then the merchant creates a checkout and completes it with the consent: the checkout is `PAID`, exactly 30.00 moved from `ada` to the shop, and the ledger sums to zero. Finally a second consent is granted, the clock is advanced 10 minutes and 1 second, and a payment is refused as `consent_expired`.

```python
def section_replay_and_tampering(stack: Stack) -> None:
    """Step 5: once only, and tamper-evident."""
    print("== 5. A replay moves money once; a changed amount is rejected")
    consent = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    before = balances(stack)
    first = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=3000, key="k-1-" + uuid.uuid4().hex[:6])
    key = first.request.headers["Idempotency-Key"]
    second = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=3000, key=key)
    after = balances(stack)
    print(f"initiate (key {key})  -> {first.status_code}, payment {first.json()['payment_id']} posted once")
    print(
        f"initiate (same key)    -> {second.status_code}, same payment {second.json()['payment_id']}, "
        f"Idempotent-Replay={second.headers.get('Idempotent-Replay')}, "
        f"ada debited {before['acct-ada'] - after['acct-ada']} minor units in total"
    )
    expect(
        first.json() == second.json() and second.headers.get("Idempotent-Replay") == "true",
        "the replay must answer the same",
    )
    expect(before["acct-ada"] - after["acct-ada"] == 3000, "a replay must not move money twice")
    another = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=3000, key="k-2-" + uuid.uuid4().hex[:6])
    print(f"a new key under the same single-payment consent -> {error_line(another)}")
    expect(another.json()["error"]["code"] == "consent_already_used", "a single-payment consent is used once")
    fresh = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    tampered = raw_payment(
        stack, consent_id=fresh["consent_id"], amount_minor=3500, signed_minor=3000, key="k-3-" + uuid.uuid4().hex[:6]
    )
    print(f"amount=35.00, signature covers amount=30.00 -> {error_line(tampered)}")
    expect(tampered.json()["error"]["code"] == "request_not_as_signed", "a changed amount must be detected")
    expect(balances(stack)["acct-ada"] == after["acct-ada"], "a tampered request must move no money")
    print()
```

Step 5: once only, and tamper-evident. The same payment with the same key twice gives the same answer (`Idempotent-Replay: true`) and moves money once; a new key under the same single-payment consent is `consent_already_used`; a request whose amount (35.00) differs from the one signed (30.00) is `request_not_as_signed` and moves no money.

```python
def section_failure_paths(stack: Stack) -> None:
    """Step 6: abandonment, revocation, a lost response."""
    print("== 6. Failure paths leave a consistent state")
    response = raw_payment(stack, consent_id=str(uuid.uuid4()), amount_minor=3000, key="s6-" + uuid.uuid4().hex[:6])
    print(f"abandoned authentication: no consent exists; initiate against a fabricated id -> {error_line(response)}")
    expect(response.json()["error"]["code"] == "consent_not_found", "no consent, no payment")
    revoked = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    before = balances(stack)
    gone = stack.http.post(
        f"{stack.bank_url}/v1/consents/{revoked['consent_id']}/revoke", json={"username": "ada", "pin": stack.ada.pin}
    )
    response = raw_payment(stack, consent_id=revoked["consent_id"], amount_minor=3000, key="s6-" + uuid.uuid4().hex[:6])
    print(f"revoked consent: revoke -> {gone.status_code}; initiate -> {error_line(response)}")
    expect(
        response.json()["error"]["code"] == "consent_revoked" and balances(stack) == before,
        "a revoked consent moves no money",
    )
    consent = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    before = balances(stack)
    checkout = stack.merchant.create_checkout.execute(Money(3000, "USD"))
    stack.merchant.faults.push(Fault.DROP_RESPONSE, 1)
    paid = stack.merchant.complete_checkout.execute(checkout.checkout_id, consent["consent_id"])
    after = balances(stack)
    print(
        f"bank timeout: the bank posted the payment, the answer was lost, the merchant retried with the same key -> "
        f"{paid.status.value}, payment {paid.payment_id}"
    )
    expect(before["acct-ada"] - after["acct-ada"] == 3000, "a timeout and a retry must move money exactly once")
    consent = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    before = balances(stack)
    checkout = stack.merchant.create_checkout.execute(Money(2000, "USD"))
    stack.merchant.faults.push(Fault.DROP_RESPONSE, 3)
    try:
        stack.merchant.complete_checkout.execute(checkout.checkout_id, consent["consent_id"])
    except BankUnavailableError as error:
        status = stack.merchant.checkouts.get(checkout.checkout_id)
        print(
            f"every answer lost -> the merchant gives up: {type(error).__name__}, "
            f"checkout {status and status.status.value}"
        )
    else:
        raise ScenarioError("three lost answers must exhaust the merchant's attempts")
    settled = stack.merchant.complete_checkout.execute(checkout.checkout_id, consent["consent_id"])
    after = balances(stack)
    print(
        f"completing again later -> {settled.status.value}; ada debited {before['acct-ada'] - after['acct-ada']} "
        "minor units: one payment, not two"
    )
    expect(before["acct-ada"] - after["acct-ada"] == 2000, "the unknown outcome must reconcile to exactly one payment")
    print()
```

Step 6: failure paths leave a consistent state. A payment against a made-up consent id is `consent_not_found`; a revoked consent moves no money (`consent_revoked`); with `DROP_RESPONSE` scripted once, the bank posts the payment, the answer is lost, the merchant retries with the same key and money moves exactly once; with three lost answers the merchant gives up (`BankUnavailableError`, checkout `UNKNOWN`), and completing again later reconciles to exactly one payment.

```python
def section_stretch(stack: Stack) -> None:
    """The stretch goals: a recurring cap, revocation in flight, the bank's signature on results."""
    print("== 7. Stretch: a recurring consent with a period cap, revocation in flight, signed results")
    terms = _terms(stack.ada, "acct-shop", "merchant-1", 10_000, scope="recurring", period_seconds=30 * 86400)
    consent = grant(stack, stack.ada, terms)
    first = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=6000, key="r1-" + uuid.uuid4().hex[:6])
    second = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=5000, key="r2-" + uuid.uuid4().hex[:6])
    print(
        f"recurring, cap 100.00 per 30 days: pay 60.00 -> {first.status_code}; pay 50.00 more -> {error_line(second)}"
    )
    expect(first.status_code == 201 and second.json()["error"]["code"] == "period_cap_exceeded", "the period cap")
    stack.clock.advance(timedelta(days=31))
    third = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=5000, key="r3-" + uuid.uuid4().hex[:6])
    print(f"31 days later the window has moved: pay 50.00 -> {third.status_code}")
    expect(third.status_code == 201, "a new period has room")
    outcomes = _revocation_race(stack, rounds=12)
    print(
        f"revocation in flight, {len(outcomes)} races: "
        + ", ".join(f"{name} {count}" for name, count in sorted(_tally(outcomes).items()))
    )
    expect(all(outcomes), "no payment may be made after its consent was revoked")
    signed = stack.merchant.complete_checkout.execute(
        stack.merchant.create_checkout.execute(Money(1000, "USD")).checkout_id,
        grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))["consent_id"],
    )
    print(f"the merchant verified the bank's signature on the result of payment {signed.payment_id}")
    expect(signed.payment_id is not None, "signed result")
    print()
```

Step 7, the stretch goals: a recurring consent with a cap of 100.00 per 30 days (60.00 succeeds, 50.00 more is refused as `period_cap_exceeded`, and after the clock moves 31 days a 50.00 payment succeeds); revocation in flight (see `_revocation_race`); and the merchant verifying the bank's signature on a payment result.

```python
def _tally(outcomes: list[bool]) -> dict[str, int]:
    return {"consistent": sum(outcomes), "inconsistent": len(outcomes) - sum(outcomes)}
```

Counts the consistent and inconsistent outcomes (`sum` of a list of true/false values counts the true ones).

```python
def _revocation_race(stack: Stack, rounds: int) -> list[bool]:
    """Pay and revoke at the same moment, `rounds` times. Consistent means: a payment exists only if it came first."""
    results: list[bool] = []
    for _ in range(rounds):
        consent = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 500))
        barrier = threading.Barrier(2)

        def pay(consent_id: str = consent["consent_id"], barrier: threading.Barrier = barrier) -> httpx.Response:
            barrier.wait(timeout=30)
            return raw_payment(stack, consent_id=consent_id, amount_minor=100, key="race-" + uuid.uuid4().hex[:8])

        def revoke(consent_id: str = consent["consent_id"], barrier: threading.Barrier = barrier) -> httpx.Response:
            barrier.wait(timeout=30)
            return stack.http.post(
                f"{stack.bank_url}/v1/consents/{consent_id}/revoke", json={"username": "ada", "pin": stack.ada.pin}
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            payment, revocation = pool.submit(pay), pool.submit(revoke)
            paid, revoked = payment.result(timeout=60), revocation.result(timeout=60)
        with stack.bank.uow_factory() as uow:
            stored = uow.consents.get_for_update(consent["consent_id"])
        assert stored is not None
        assert stored.revoked_at is not None
        assert revoked.status_code == 200
        if paid.status_code == 201:
            with stack.bank.uow_factory() as uow:
                payment_row = uow.payments.get(paid.json()["payment_id"], "merchant-1")
            results.append(payment_row is not None and payment_row.created_at <= stored.revoked_at)
        else:
            results.append(paid.json()["error"]["code"] == "consent_revoked")
    return results
```

Pays and revokes at the same moment, `rounds` times. Each round grants a consent, then two threads wait on a `Barrier(2)` and are released together: one makes a payment, the other revokes. The default arguments `consent_id=consent["consent_id"]` bind the current round's values to each nested function. Afterwards the consent must be revoked, and the result is consistent if a payment exists only when it was created no later than the revocation, or the payment was refused as `consent_revoked`. The consent row lock is what makes one of the two win cleanly.

```python
def section_final(stack: Stack) -> None:
    """The books after everything."""
    print("== 8. The books")
    found = balances(stack)
    print(", ".join(f"{account} {Money(minor, 'USD').format()}" for account, minor in found.items()))
    print(f"ledger sum across every posting: {Money(ledger_total(stack), 'USD').format()}")
    expect(ledger_total(stack) == 0, "the ledger must sum to zero")
    expect(
        found["acct-ada"] + found["acct-bob"] + found["acct-shop"] + found["acct-other-shop"] == 70_000,
        "no money created or lost",
    )
```

Step 8, the books: it prints the balances and the ledger sum, requires the sum to be zero and requires that the four accounts together hold exactly 700.00 (70,000 minor units): no money was created or lost.

```python
def run_scenario(out: Path) -> None:
    """Run every section."""
    with running_stack(out) as stack:
        section_trust(stack)
        consent = section_consent(stack)
        section_merchant_access(stack, consent)
        section_within_consent(stack)
        section_replay_and_tampering(stack)
        section_failure_paths(stack)
        section_stretch(stack)
        section_final(stack)
```

Runs every section inside `running_stack`, passing the consent from step 2 to step 3.

```python
def main(argv: Sequence[str] | None = None) -> int:
    """Run the scenario; exit 0 when it behaved, 1 when the system broke a rule, 2 for a configuration error."""
    parser = argparse.ArgumentParser(prog="open-banking-demo", description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path("var"), help="where the demo's keys and files go (default var/)"
    )
    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    configure_logging("ERROR")
    try:
        run_scenario(args.out)
    except ConfigurationError as error:
        print(f"configuration error: {error}")
        return 2
    except ScenarioError as failure:
        print(f"FAILED: {failure}")
        return 1
    return 0
```

Runs the scenario. `--out` says where the demo's keys and files go (default `var/`). Output is line-buffered; logging is set to `ERROR` so only the printed sections appear. Exit codes: 0 when it behaved, 1 when the system broke a rule (`FAILED: ...`), 2 for a configuration error.

```python
if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
```

Exits with the status `main()` returns.

## Try it yourself

1. Run the whole lesson against the database (needs `make up` first):

   ```bash
   make up
   make run
   ```

   Expected: eight numbered sections; the last lines show the balances and `ledger sum across every posting: 0.00`; exit status 0.

2. Start the bank and ask for its health:

   ```bash
   make bank &
   make health
   ```

   Expected: `{"status":"ok","checks":{"database":"ok"}}`. Stop it with `kill %1` and check it exits cleanly.

3. Read the bank's public signing key: `curl -s http://127.0.0.1:8463/.well-known/bank-keys.json` while the bank runs. Expected: `{"keys":[{"kty":"EC","crv":"P-256","x":...,"y":...}]}`.

## Mistakes people make

- **Putting business rules in a route function.** Routes should call a use case and return its view.
- **Trusting the `Host` header to build the URL a proof is bound to.** The bank uses its configured public URL.
- **Reading a form with a heavy library for one field.** The token endpoint reads it with `parse_qs`.
- **Leaking internals in an error.** The catch-all handler returns a fixed message and logs the details.
- **Printing a secret.** The admin program writes secrets to owner-only files and prints only file names.

## Check yourself

1. What do the four headers of `POST /v1/payments` do? *`Authorization` carries the DPoP token, `DPoP` the proof of possession, `Idempotency-Key` the once-only label and `X-Request-Signature` the merchant's signature over the request.*
2. Why does the bank sign its answers? *So the merchant can check that a payment result really came from the bank.*
3. What does `--abandon` do in the customer program? *Stops after the code is sent, so no consent is created.*
4. Why does the revocation race use a Barrier? *To release the payment and the revocation at the same instant.*
5. What exit codes does the demo use? *0 behaved, 1 the system broke a rule, 2 configuration error.*

## Further reading

- FastAPI documentation: https://fastapi.tiangolo.com/
- Pydantic documentation: https://docs.pydantic.dev/
- Python 3.12 `argparse`: https://docs.python.org/3.12/library/argparse.html
- uvicorn: https://www.uvicorn.org/
- RFC 9449, DPoP: https://www.rfc-editor.org/rfc/rfc9449
