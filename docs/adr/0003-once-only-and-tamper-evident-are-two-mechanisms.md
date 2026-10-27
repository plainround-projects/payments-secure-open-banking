# 3. "Once only" and "tamper-evident" are two mechanisms

Status: accepted, 2026-10-27

## Context

A timeout makes the merchant retry. An intermediary may change the amount. These are different threats and need different answers; mixing them hides both.

## Decision

**Once only.** The merchant sends an `Idempotency-Key` it chooses (built from the checkout and the consent, so a retry of the same attempt always sends the same key). The bank scopes keys to the authenticated merchant, takes a transaction-scoped advisory lock on (merchant, key), and stores the response under the key in the same transaction as the payment. A repeat returns the stored response and checks nothing again; a key reused with a different request is refused with 422. Only successes are stored: a refusal rolls everything back, so the same key can be tried again once the cause is fixed. **Tamper-evident.** The merchant signs the exact request (merchant, consent, payee, amount, currency and the key) with its registered key; the bank verifies the signature before it reads the consent. A changed amount fails with `request_not_as_signed`, whatever the amount is. The signature covers the key, so it cannot be moved onto another attempt.

## Alternatives considered

- **The key alone.** A retried request cannot move money twice, but nothing detects an altered amount.
- **The signature alone.** A genuine, signed request can be replayed.
- **Global idempotency keys.** One merchant's key could collide with, or probe, another's.

## Consequences

- A timeout followed by a retry reconciles to one payment: the bank either does the work or returns what it stored.
- The signature protects the request in transit between merchant and bank; a compromised merchant that signs anything is bounded by the consent's cap, payee, scope and expiry.
- The bank also signs its result (ES256, key published at `/.well-known/bank-keys.json`); the merchant's client checks it.
