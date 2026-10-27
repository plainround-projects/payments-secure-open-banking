# 1. A consent exists only against a proof of strong authentication, tied to its terms

Status: accepted, 2026-10-27

## Context

The lesson's central rule: money moves only with a consent the customer gave, after strong authentication, directly to the bank. A consent that could be created without that, or created for terms the customer never saw, would be a payment authority with nothing behind it.

## Decision

Authentication is a challenge in two factors (the PIN, then a one-time code sent to the customer's device) that is created for exact terms: payer, payee, merchant, currency, cap, scope and period. The challenge stores a keyed digest of the code, never the code, locks after three wrong codes and expires after five minutes. A right code issues a **proof**: an unguessable bearer value (stored as a digest) that carries the digest of the terms, expires after two minutes and can be used once. `Consent.grant` takes the proof and the terms and refuses unless the proof is unspent, unexpired and was issued for these terms (dynamic linking), so a proof for 50.00 cannot create a consent for 5,000.00. The proof is spent in the same transaction that creates the consent. Revoking needs only the PIN: withdrawing authority needs one factor, granting it needs two.

## Alternatives considered

- **A boolean "authenticated" on a session.** Says nothing about what was approved.
- **A one-time code as the proof itself.** It would be reusable by anyone who saw it, for any terms.
- **The merchant relays the code.** Breaks the trust model: the merchant must never see the PIN or the code. The merchant package cannot import the bank's authentication code, which `make lint` enforces.

## Consequences

- Abandoned authentication leaves a challenge and nothing else: no proof, no consent.
- The consent carries its scope and its expiry (ten minutes for a single payment, ninety days for a recurring consent) in columns you can query.
- The one-time code travels only through the stand-in phone (a file readable by its owner); the bank's logs and database never hold it.
