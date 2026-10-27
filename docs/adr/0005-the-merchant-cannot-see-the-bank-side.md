# 5. The merchant cannot reach the bank's side of the trust model, and the code says so

Status: accepted, 2026-10-27

## Context

"Strong customer authentication happens between the customer and the bank, never relayed through the merchant" is the lesson's first design rule. A diagram can claim it; code can enforce it.

## Decision

The merchant is its own set of modules: `application/merchant`, `interfaces/merchant_api`, `interfaces/merchant_container`, `interfaces/serve_merchant`, and its client in `infrastructure/bank_client`. An import-linter contract forbids those modules (and everything they import, directly or not) from reaching `application/bank`, `domain/authentication`, the one-time-code channel, the bank's API or the customer program. The merchant has no database: its checkouts live in memory, and its only credentials are its own key and secret. The shared ports are split so that the merchant side does not import the bank's ports, and settings are split into `settings` (shared and merchant) and `bank_settings`. The contract caught a real coupling while the project was built (a shared composition root that imported the one-time-code channel), which is why the composition roots are two files.

## Alternatives considered

- **A convention and a review checklist.** Erodes silently.
- **Two repositories.** The clearest boundary and the worst teaching experience for a single lesson; the contract gives the same guarantee in one tree.

## Consequences

- The merchant's calls to the bank are exactly `POST /oauth/token`, `POST /v1/payments` and `GET /v1/payments/{id}`, and nothing in the merchant's code can ask for a PIN or a code.
- A change that adds such an import fails `make lint`.
