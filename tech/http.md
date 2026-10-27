# HTTP

## What it is

HTTP (HyperText Transfer Protocol) is the set of rules clients and servers use to talk over a network. A client sends a **request**; the server sends back a **response**. Each message is plain text with a start line, **headers** (name: value pairs) and an optional **body**. In this repository HTTP carries everything: a customer program talks to the bank, a merchant talks to the bank, and a person's browser or program talks to the merchant.

## Why this repository uses it

Open banking is, at its core, a set of HTTP conversations whose headers carry the security: who is calling (`Authorization`), proof that the caller holds a key (`DPoP`), a request signature (`X-Request-Signature`), the key that makes a payment happen once (`Idempotency-Key`), and the bank's signature on the answer (`Bank-Signature`). The status codes tell the caller exactly what kind of refusal it was.

## Where it appears

| Path | Role |
| --- | --- |
| `src/.../interfaces/bank_api.py` | The bank's routes, headers and status codes. |
| `src/.../interfaces/merchant_api.py` | The merchant's routes. |
| `src/.../infrastructure/bank_client.py` | The merchant's HTTP client for the bank. |
| `src/.../interfaces/customer_cli.py` | The customer's program, a client of the bank. |
| `README.md` | The endpoint tables. |

## How it works underneath

A request on the wire:

```
POST /v1/payments HTTP/1.1
Host: localhost:8463
Authorization: DPoP <access token>
DPoP: <signed proof>
Idempotency-Key: pay-0001
X-Request-Signature: <signature>
Content-Type: application/json

{"consent_id": "...", "payee_account": "acct-shop", "amount_minor": 3000, "currency": "USD"}
```

- **Method**: what to do. `GET` reads and must not change anything; `POST` triggers an action.
- **Status code** in the response: 2xx success, 4xx the caller's fault, 5xx the server's or a dependency's problem.
- A **TCP connection** carries the messages; HTTP/1.1 can reuse one connection for many requests.
- Headers are the right place for credentials and proofs, because they travel with every request and are not part of the URL that gets logged.

## Walkthrough of this repository's use

### The bank's endpoints

| Method and path | Caller | Meaning |
| --- | --- | --- |
| `POST /v1/sca/challenges` | customer | Start strong customer authentication: username, PIN and the terms about to be approved. A one-time code is "sent". |
| `POST /v1/sca/challenges/{id}/verify` | customer | Enter the code. A right code returns a single-use proof (valid 2 minutes); a wrong one counts; the third locks the challenge. |
| `POST /v1/consents` | customer | Create a consent. Requires `Authorization: SCA <proof>` for exactly these terms. |
| `POST /v1/consents/{id}/revoke` | customer | Withdraw a consent (needs the PIN). |
| `POST /oauth/token` | merchant | Exchange client id and secret, plus a proof of possession, for an access token bound to the merchant's key. |
| `POST /v1/payments` | merchant | Initiate a payment under a consent. Needs the token, a proof, `Idempotency-Key` and `X-Request-Signature`. |
| `GET /v1/payments/{id}` | merchant | Read a payment of your own. |
| `GET /.well-known/bank-keys.json` | anyone | The bank's public signing key. |
| `GET /health/live`, `GET /health` | platform | Is the process up; is it and its database healthy. |

### The merchant's endpoints

`POST /v1/checkouts` (201), `POST /v1/checkouts/{id}/complete`, `GET /v1/checkouts/{id}`, and the health routes.

### The authorization schemes

- `Authorization: Basic base64(client_id:secret)` on the token request (the standard HTTP Basic scheme; the secret is not hashed on the wire, so the bank must be reached over TLS in a real deployment).
- `Authorization: DPoP <token>` on payment requests, with a `DPoP: <proof>` header.
- `Authorization: SCA <proof>` when a customer creates a consent. `SCA` is this lesson's own scheme name for the single-use proof of strong customer authentication.

### Status codes (from `bank_api.py`)

| Status | When |
| --- | --- |
| 200 / 201 | Success; 201 for created things (challenge, consent, payment). |
| 400 | Invalid body or money, a missing idempotency key, or `request_not_as_signed`. |
| 401 | Failed authentication: wrong or missing credential, bad proof, wrong or locked or expired code (with `WWW-Authenticate: DPoP error="invalid_token"` for merchants). |
| 403 | A refusal by the consent rules (`consent_revoked`, `amount_exceeds_consent`, `payee_not_covered`, `consent_not_granted_to_caller`, ...) or an account that is not the caller's. |
| 404 | Unknown consent, challenge, payment or account. |
| 409 | A challenge that was already used. |
| 422 | A reused idempotency key with a different request, `insufficient_funds`, or an unknown client. |
| 502 / 504 | Merchant side: the bank could not authenticate the merchant (502) or did not answer (504, retryable). |
| 503 | `/health`: the database is unreachable. |

Errors share one shape: `{"error": {"code": "...", "message": "..."}}`, sometimes with extra fields such as `attempts_left`.

### Headers beyond authentication

- `Idempotency-Key` (request): 1 to 128 characters; scoped **per merchant**, so two merchants may use the same key text.
- `Idempotent-Replay: true` (response): the answer is the stored copy of an earlier identical request; nothing happened this time.
- `Bank-Signature` (response): the bank's ES256 signature over the exact JSON body.
- `Cache-Control: no-store` on responses that carry tokens or proofs.
- `X-Request-Id` (both ways): a trace id.

### Why a retry is safe

A merchant that gets no answer to a payment resends the **same request with the same key**. The bank either does the work now or replays the answer it stored, so the payer is charged once. The merchant's client does this up to three times with a growing pause.

## Try it yourself

1. Read the bank's public key (with the bank running: `make up`, `make register`, `make serve-bank`):

   ```bash
   curl -s localhost:8463/.well-known/bank-keys.json
   ```

   Expected: `{"keys":[{"kty":"EC","crv":"P-256","x":"...","y":"..."}]}` (the key members only).

2. Call a protected endpoint without credentials:

   ```bash
   curl -i -X POST localhost:8463/v1/payments -H 'Content-Type: application/json' -d '{"consent_id":"x","payee_account":"acct-shop","amount_minor":100,"currency":"USD"}'
   ```

   Expected: `HTTP/1.1 401`, a `www-authenticate: DPoP error="invalid_token"` header and the body `{"error":{"code":"invalid_client_credential","message":"invalid merchant credential"}}`.

3. See the whole conversation, with every status and refusal reason: `make up && make run`.

## Mistakes people make

- **Putting secrets in URLs or logging headers.** URLs and headers are logged by proxies; this code logs neither.
- **Using 200 for everything.** Distinct statuses and codes drive retry and user-interface decisions.
- **Retrying a `POST` without an idempotency key.** The payer could be charged twice.
- **Answering authentication failures with detail.** Which part was wrong helps only an attacker.
- **Skipping TLS.** This teaching build uses plain HTTP on localhost; a real bank requires TLS everywhere.

## Check yourself

1. Which header makes a payment retry safe? *`Idempotency-Key`.*
2. What does a 403 with `amount_exceeds_consent` tell the merchant? *The request was understood and authenticated, but the consent does not allow that amount.*
3. What does 401 mean here? *Authentication failed (credential, proof, or code).*
4. What is `Bank-Signature` for? *Letting the merchant verify the result came from the bank.*
5. Why does the Makefile-started demo not use TLS? *It runs on one machine for teaching; production needs TLS.*

## Further reading

- HTTP Semantics (RFC 9110): https://www.rfc-editor.org/rfc/rfc9110
- HTTP authentication, `WWW-Authenticate` (RFC 9110, section 11): https://www.rfc-editor.org/rfc/rfc9110#section-11
- DPoP (RFC 9449): https://www.rfc-editor.org/rfc/rfc9449
- OAuth 2.0 client credentials (RFC 6749, section 4.4): https://www.rfc-editor.org/rfc/rfc6749#section-4.4
- MDN, HTTP overview: https://developer.mozilla.org/en-US/docs/Web/HTTP
