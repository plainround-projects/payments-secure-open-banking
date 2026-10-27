# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-27

### Added

- PostgreSQL 18 in `docker-compose.yml`; the bank as an optional container (profile `app`).
- SQL scripts: the schema (accounts, an append-only double-entry ledger whose balances the database checks against the postings, customers, authentication challenges and proofs, consents, merchants, tokens, proofs of possession, payments, idempotency keys), the seed (the lesson's accounts, no credentials), the lesson's queries, a reset script, and four stepwise migrations that build the same schema.
- A mock bank: strong customer authentication in two steps (PIN, then a one-time code sent to the customer's device), tied to the terms being approved; consent as an object created only against a valid proof; revocation by the customer.
- Merchant access: client credentials exchanged for a short-lived token bound to the merchant's own key (DPoP), with a fresh proof of possession for every request and a table of proofs already used.
- Payment initiation: every rule of the consent checked with its own reason, a signed request, an idempotency key scoped to the merchant and stored with the effect, the bank's signature on the result, and a recurring scope with a cap per period.
- A merchant service with its own checkout and a client that retries with the same key; a fault layer that loses a response after the bank has committed.
- Programs: the bank and merchant servers (graceful shutdown), the admin program, the customer program and `open-banking-demo` (the lesson's whole scenario).
- Unit tests (with a property test over random sequences), integration tests against PostgreSQL (races included), end-to-end tests against real server processes; ruff, mypy, import-linter (including a contract that keeps the merchant away from the bank's authentication code), hash-pinned lock files and a CI workflow.
