# 2. Merchant access: client credentials, and tokens bound to the merchant's own key

Status: accepted, 2026-10-27

## Context

The merchant must prove who it is, separately from the customer's consent. A plain bearer token is enough for whoever steals it. The lesson asks for a token that is useless without the merchant's own key.

## Decision

A registered merchant has a client secret (stored as a slow salted hash) and a P-256 key pair (the bank holds the public half). To get a token it presents the secret **and** a proof of possession (a signed JWT in the style of DPoP, RFC 9449) for the token request. The bank issues an opaque token, stores only its keyed digest and the thumbprint of the key it is bound to, and the token lives ten minutes. Every call to the payments API carries the token and a fresh proof: signed by that key, naming the method and the URL, the time (within 60 seconds) and the hash of the token. The proof's id is recorded; a proof used twice is refused. A stolen token without the key, a proof for another URL, an old proof and a replayed proof are each refused.

## Alternatives considered

- **A bearer token.** The gap this decision closes.
- **mTLS-bound tokens.** Equally sound and needs certificate handling that a teaching build would mostly spend on ceremony; the same binding idea.
- **Signed JWT access tokens.** Would need the bank to guard a signing key and handle revocation; opaque tokens in the database revoke by deleting a row.

## Consequences

- A leaked access token alone cannot be used; a leaked key alone cannot get one without the secret.
- Every payment request costs one signature verification and one insert (the proof id).
- The "htu" is the bank's public URL from configuration, not the request's Host header, so a proof cannot be redirected by a forged header.
