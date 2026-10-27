# 4. The locks that keep a payment consistent under races

Status: accepted, 2026-10-27

## Context

Two merchants' requests, two payments on one consent, a payment and a revocation, and two payments in opposite directions can all arrive at once. Each must end in a state the rules allow.

## Decision

Every payment is one transaction that takes locks in a fixed order: the advisory lock on (merchant, key); the consent row `FOR UPDATE`; then the payer and payee account rows `FOR UPDATE`, sorted by account id. Revoking takes the same consent lock. So a payment and a revocation run one after the other: a payment that locks the consent first completes before the revocation (and its time is at or before the revocation's); one that locks it second finds it revoked and is refused. A single-payment consent is marked used under the lock, so twenty payments with different keys produce one. The payer's balance is checked under its lock, so concurrent payments cannot overdraw it, and sorted locking prevents a deadlock between payments in opposite directions. The database repeats these rules as constraints: a balance that disagrees with the postings, a customer account below zero, a second ledger entry for one payment, and an unbalanced or edited posting cannot be committed.

## Alternatives considered

- **Optimistic checks with retries.** More code for the same result at this size.
- **Serializable isolation for everything.** Correct, and it would move the burden to retrying aborted transactions in every use case.

## Consequences

- The integration tests run each race (20 equal keys, 20 different keys, 20 payments against one balance, 16 opposite-direction payments, 6 payment-and-revocation races) against PostgreSQL.
- A hot account serializes its payments; production watches lock waits on it.
