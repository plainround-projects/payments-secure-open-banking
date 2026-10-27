# httpx

## What it is

`httpx` is a Python library for **making** HTTP requests: you give it a URL, a method and optional headers and a body, and it returns a response object. (FastAPI and uvicorn receive requests; httpx sends them.) It has a one-shot form (`httpx.get(url)`) and a reusable `httpx.Client` that keeps connections open. A **transport** is the lower layer that actually moves bytes; httpx lets you wrap it.

## Why this repository uses it

The merchant is a *client* of the bank, and the lesson's reliability story lives in that client: authenticate with a key-bound token, sign every request, **retry with the same idempotency key** when the bank does not answer, and check the bank's signature on the result. The demo and tests also need clients of both services, and a way to make the network misbehave on purpose (lose a response after the bank has paid).

## Where it appears

| Path | Use |
| --- | --- |
| `src/open_banking/infrastructure/bank_client.py` | `HttpBankGateway`: token, proof, signature, retries, signature check. |
| `src/open_banking/infrastructure/faults.py` | `FaultInjectingTransport`: a transport wrapper that drops a request or loses a response on command. |
| `src/open_banking/interfaces/customer_cli.py`, `demo_cli.py` | Clients of the bank and of the merchant. |
| `src/open_banking/interfaces/health_cli.py` | The container health check. |
| `tests/e2e/` | Clients of real server processes. |
| `requirements.lock` | `httpx==0.28.1`, `httpcore==1.0.9`, `h11==0.16.0`. |

## How it works underneath

An HTTP request is text over a TCP connection. httpx builds it, sends it through a **transport**, and parses the reply. A `Client` keeps a **connection pool**. `httpx.TransportError` is the family of errors where no usable answer arrived (connect or read timeouts, dropped connections); an error *status* such as 401 is an answer, not an exception.

## Walkthrough of this repository's use

### Authenticating: a token and a proof

```python
response = self._client.post(
    url,
    data={"grant_type": "client_credentials"},
    auth=(self._client_id, self._client_secret),
    headers={"DPoP": proof},
)
```

- `data={...}` sends a form body (`application/x-www-form-urlencoded`), as the OAuth token endpoint expects.
- `auth=(id, secret)` makes httpx add the `Authorization: Basic ...` header.
- `headers={"DPoP": proof}` adds the proof of possession built for this URL.
- The reply holds the access token and its lifetime. The client caches it and refreshes 30 seconds early (`lifetime = max(int(payload["expires_in"]) - 30, 1)`), and forgets it after a 401.

### Sending a payment

```python
return self._client.post(
    url,
    json=body,
    headers={
        "Authorization": f"DPoP {token}",
        "DPoP": proof,
        "Idempotency-Key": idempotency_key,
        "X-Request-Signature": signature,
    },
)
```

`json=body` serializes the dict and sets the content type. Every attempt gets a **fresh proof** (the bank refuses a proof it has seen), but the **same** idempotency key and signature.

### The retry loop

```python
for attempt in range(1, self._attempts + 1):
    try:
        response = self._post_payment(body, idempotency_key, signature)
    except httpx.TransportError as error:
        last_error = type(error).__name__
        logger.warning("bank did not answer; retrying with the same key", extra=fields(...))
    else:
        if response.status_code == 401:
            self._token = None
            last_error = "401"
        elif response.status_code in _RETRYABLE_STATUSES:     # {502, 503, 504}
            last_error = str(response.status_code)
        else:
            return self._result(response)
    if attempt < self._attempts:
        self._sleep(self._base_delay * 2 ** (attempt - 1))
raise BankUnavailableError(...)
```

- A transport error, a 502/503/504, or a 401 (an expired token, so a new one is fetched) is retried; any other answer, including a refusal like 403, is final.
- The pause doubles each time (`base * 2**(attempt-1)`, **exponential backoff**).
- After the last attempt the client raises `BankUnavailableError`: the payment may or may not have been made, and the merchant's checkout becomes `UNKNOWN`. Asking again is safe because of the idempotency key.
- The `try ... except ... else` form: `else` runs only when no exception happened.

### Checking the bank's signature

```python
signature = response.headers.get("Bank-Signature", "")
message = json.dumps(response.json(), sort_keys=True, separators=(",", ":")).encode()
if not keys.verify(self._bank_public_jwk, message, signature):
    raise ClientAuthenticationError("the payment result is not signed by the bank")
```

The client re-creates the canonical bytes of the body and verifies the header with the bank's public key (see `jwt-and-dpop.md`).

### Making the network fail on purpose

```python
class FaultInjectingTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        fault = self._script.next()
        if fault is Fault.DROP_REQUEST:
            raise httpx.ConnectTimeout("scripted: the request never reached the bank", request=request)
        response = self._inner.handle_request(request)
        if fault is Fault.DROP_RESPONSE:
            response.read()
            response.close()
            raise httpx.ReadTimeout("scripted: the bank processed the request but its answer was lost", request=request)
        return response
```

A `BaseTransport` wrapper around the real one. With `DROP_REQUEST` the request is never sent; with `DROP_RESPONSE` it **is** sent, the bank commits the payment, the response is read and thrown away, and a read timeout is raised, exactly what a lost reply looks like to a caller. The retry then resends with the same key, and the demo shows the bank replaying the stored answer with money moved once.

### The health check

`health_cli.py` asks `HEALTH_URL` (default: the bank at `BANK_HOST:BANK_PORT`, path `/health`) with `httpx.get(url, timeout=5.0)`, prints the body and exits 0 for 200, else 1. It is the container health check.

## Try it yourself

1. Run the failure scenario, which uses the fault transport (with `make up`):

   ```bash
   make run
   ```

   Expected: section `== 6. Failure paths leave a consistent state` includes lines starting `bank timeout: the bank posted the payment, the answer was lost, the merchant retried with the same key -> PAID, payment ...` and `every answer lost -> the merchant gives up: BankUnavailableError, checkout UNKNOWN`, then `completing again later -> PAID; ada debited 2000 minor units: one payment, not two`. The last line is `ledger sum across every posting: 0.00`.

2. Run the unit tests of the retry helper and the checkout's unknown outcome: `.venv/bin/python -m pytest tests/unit -q -k "retry or unknown"`.

3. With the bank running: `make health`. Expected: `{"status":"ok","checks":{"database":"ok"}}`.

## Mistakes people make

- **Retrying without the same idempotency key,** which could pay twice.
- **Reusing a proof** on a retry; the bank refuses a seen `jti`, so every attempt builds a new one.
- **Retrying forever.** The attempts are capped (default 3) and the pause grows.
- **Treating a 403 as retryable.** A refusal is an answer.
- **No timeout.** The merchant's client sets a request timeout (`BANK_TIMEOUT_SECONDS`, default 5).

## Check yourself

1. Why does each retry use a fresh DPoP proof but the same idempotency key? *The bank rejects reused proofs, but the key is what makes the payment happen once.*
2. Which results are retried? *Transport errors, 502/503/504, and a 401 after dropping the cached token.*
3. What does `DROP_RESPONSE` simulate? *The bank paid, but the answer never reached the merchant.*
4. What does the client do with the `Bank-Signature` header? *Verifies it against the bank's public key over the canonical body.*
5. What is exponential backoff? *Waiting longer after each failure (here doubling).*

## Further reading

- httpx (0.28): https://www.python-httpx.org/
- Clients and transports: https://www.python-httpx.org/advanced/transports/
- Authentication: https://www.python-httpx.org/advanced/authentication/
- Timeouts: https://www.python-httpx.org/advanced/timeouts/
- Exceptions: https://www.python-httpx.org/exceptions/
