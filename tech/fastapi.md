# FastAPI

## What it is

FastAPI is a Python **web framework**: a library that turns ordinary Python functions into an HTTP API. You say "a POST to `/v1/consents` runs this function", and FastAPI reads the request, checks the data, calls your function and writes the response. It is built on **Starlette** (routing, middleware, responses, the test client) and **Pydantic** (data checking; see `pydantic.md`).

## Why this repository uses it

The lesson has two services that talk to each other over HTTP: a **bank** (customer authentication, consents, merchant tokens, payments) and a **merchant** (checkouts). FastAPI builds both with the same few lines of plumbing, reads the security headers (`Authorization`, `DPoP`, `Idempotency-Key`, `X-Request-Signature`), and its `TestClient` lets each service be tested without opening a network port.

## Where it appears

| Path | What it does with FastAPI |
| --- | --- |
| `src/open_banking/interfaces/bank_api.py` | `create_bank_app()`: the bank's routes, request bodies, error handlers. |
| `src/open_banking/interfaces/merchant_api.py` | `create_merchant_app()`: the merchant's routes. |
| `src/open_banking/interfaces/http_common.py` | `install_common()`: request logging middleware, the validation handler and the catch-all, shared by both apps. |
| `tests/unit/interfaces/test_bank_api.py`, `test_merchant_api_and_clis.py` | Use `fastapi.testclient.TestClient`. |
| `requirements.lock` | `fastapi==0.162.0`, `starlette==1.10.0`. |

## How it works underneath

1. A server program (uvicorn, see `uvicorn.md`) accepts a TCP connection and parses the HTTP request.
2. It calls the FastAPI object as an **ASGI application**: a standard Python interface where the server passes the request details and the app sends back response pieces.
3. FastAPI matches the path and method to a **route** and runs the **middleware** chain around it.
4. For the matched function it reads each parameter from the right place (path, query, header, JSON body) and converts and checks it by its type hint. A failure raises `RequestValidationError` before your function runs.
5. Routes defined with plain `def` run in a **worker thread**; routes defined with `async def` run on the event loop.
6. The return value becomes the response; exceptions go to the registered **exception handlers**.

## Walkthrough of this repository's use

### Two apps, one shape

```python
def create_bank_app(container_factory: Callable[[], BankContainer]) -> FastAPI:
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

A function builds the app so tests can pass fakes. The **lifespan** (an async context manager) opens the container (database pool, keys, use cases) once at startup and closes it at shutdown. `docs_url=None` and `redoc_url=None` switch off the built-in interactive docs pages. `install_common(app)` adds the pieces both services share.

### Reading headers, the security part

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
    result = bank(request).initiate_payment.execute(client_id, IdempotencyKey(idempotency_key), PaymentRequest(...), signature)
```

- `body: PaymentBody` is a Pydantic model; FastAPI parses and checks the JSON body into it.
- `Annotated[str | None, Header(alias="DPoP")] = None` reads the request header named exactly `DPoP`. Without `alias`, FastAPI would derive the name from the parameter (`dpop` -> `dpop`); header names are case-insensitive but the alias keeps the intent explicit. `authorization` needs no alias.
- Every header is optional at the FastAPI level (default `None`); the use cases refuse a missing or invalid one with a clear, named error. That keeps all security decisions in code that is tested without HTTP.
- `caller(...)` authenticates the merchant first (token and proof), **before** anything else about the request is considered.
- `status_code=201` is the success status.

### Why some routes are `async` and some are not

```python
@app.post("/oauth/token")
async def token(request: Request, authorization: ..., dpop: ...) -> JSONResponse:
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"), keep_blank_values=True)
    ...
    issued = await run_in_threadpool(bank(request).issue_token.execute, client_id, secret, dpop, url)
```

The token endpoint receives a standard **form body** (`grant_type=client_credentials`), which FastAPI normally parses with an extra package (`python-multipart`). One field does not justify a dependency, so the route is `async` to `await` the raw body, parses it with the standard library's `parse_qs`, and then calls the (blocking) database code with `run_in_threadpool`, which runs it on a worker thread without blocking the event loop. The other routes are plain `def`, which FastAPI already runs in threads.

### Middleware and shared handlers

```python
@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    ...
    logger.info("request", extra=fields(request_id=request_id, method=request.method, path=request.url.path, status=response.status_code, duration_ms=...))
```

Method, path, status, duration and a request id, and **never** bodies or headers, because the headers carry tokens and proofs and the bodies carry PINs and codes. `install_common` also registers a handler that turns `RequestValidationError` into a 400 (`invalid_request`) and a catch-all that logs the exception and answers a generic 500.

### Error handlers with security in mind

```python
@app.exception_handler(OpenBankingError)
async def named_error(request: Request, error: OpenBankingError) -> JSONResponse:
    status = _STATUS.get(type(error), 500)
    response = error_response(error.code, error.message, status)
    if status == 401 and isinstance(error, ClientAuthenticationError):
        response.headers["WWW-Authenticate"] = 'DPoP error="invalid_token"'
    return response
```

A table maps each named error to a status (401 for failed authentication, 403 for another customer's account, 404 for missing things, 422 for a reused idempotency key). A separate handler for `PaymentRefusedError` returns the **distinct refusal code** and message (`amount_exceeds_consent`, `consent_revoked`, ...). A failed merchant authentication also sets the `WWW-Authenticate` header, which tells a client how to authenticate.

### Responses with signed bodies

```python
payload = dict(result.response.body)
headers = {"Bank-Signature": keys.sign(bank(request).signing_pem, _canonical(payload))}
if result.replayed:
    headers["Idempotent-Replay"] = "true"
return JSONResponse(payload, status_code=result.response.status, headers=headers)
```

The bank signs the exact JSON of each payment result (see `jwt-and-dpop.md`), and marks a replay.

### The merchant app

`/v1/checkouts` (create), `/v1/checkouts/{id}/complete` (ask the bank to pay under a consent the customer gave) and `GET /v1/checkouts/{id}`. A refused completion answers 403 with the bank's reason; a bank that did not answer is 504 with `retryable: true`.

### Testing

`TestClient(create_bank_app(lambda: container))` calls the app in-process. The end-to-end tests and the demo start real server processes instead (see `uvicorn.md`).

## Try it yourself

1. Run the HTTP and command-line unit tests:

   ```bash
   .venv/bin/python -m pytest tests/unit/interfaces -q
   ```

   Expected: `38 passed` (the count grows if tests are added).

2. List the bank's routes without a server:

   ```bash
   .venv/bin/python - <<'EOF'
   from open_banking.interfaces.bank_api import create_bank_app
   for route in create_bank_app(lambda: None).routes:
       print(sorted(getattr(route, "methods", []) or []), route.path)
   EOF
   ```

   Expected: eleven lines: `['GET', 'HEAD'] /openapi.json` (FastAPI adds it; only the human-readable docs pages are off), then `POST /oauth/token`, `POST /v1/sca/challenges`, `POST /v1/sca/challenges/{challenge_id}/verify`, `POST /v1/consents`, `POST /v1/consents/{consent_id}/revoke`, `POST /v1/payments`, `GET /v1/payments/{payment_id}`, `GET /.well-known/bank-keys.json`, `/health/live` and `/health`.

3. Run the whole scenario: `make up`, then `make run`. It starts both services inside one process and prints each section's result. Expected: the last line shows `ledger sum across every posting: 0.00`.

4. Start the services by hand: `make register`, then `make serve-bank` (one terminal) and `make serve-merchant` (another); call `curl -s localhost:8463/.well-known/bank-keys.json`. Expected: a JSON object `{"keys": [ {...public key...} ]}`.

## Mistakes people make

- **Blocking calls in `async def` routes.** They freeze the event loop; use `def` routes or `run_in_threadpool`.
- **Logging headers or bodies.** Here they hold tokens, proofs, PINs and codes.
- **Telling the caller too much.** Failed merchant authentication says only `invalid merchant credential`.
- **Doing authorization after reading the body.** The bank authenticates the caller first.
- **Opening the database at import time.** The lifespan opens it when the app really starts.

## Check yourself

1. What does `Header(alias="DPoP")` do? *Reads the header named `DPoP` into the parameter.*
2. Why is the token route `async` while the others are `def`? *It awaits the raw form body; the blocking database work is sent to a thread with `run_in_threadpool`.*
3. What does the lifespan do? *Opens the container at startup and closes it at shutdown.*
4. Why are headers never logged? *They carry credentials and proofs.*
5. What does the `Bank-Signature` header let the merchant do? *Verify that the result really came from the bank.*

## Further reading

- FastAPI documentation (0.162): https://fastapi.tiangolo.com/
- Header parameters: https://fastapi.tiangolo.com/tutorial/header-params/
- Lifespan events: https://fastapi.tiangolo.com/advanced/events/
- Handling errors: https://fastapi.tiangolo.com/tutorial/handling-errors/
- Concurrency (`async def` vs `def`): https://fastapi.tiangolo.com/async/
- Starlette (1.10): https://www.starlette.io/
