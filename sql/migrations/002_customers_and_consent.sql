-- Steps 1 and 2 of the lesson: strong customer authentication, and consent as an object that exists only after it.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a credit to the account
-- and a negative amount is a debit. The database refuses an unbalanced entry, an edit to the ledger, a balance that
-- disagrees with the postings, an overdrawn customer account, and a second payment under one idempotency key.

BEGIN;

CREATE TABLE customers (
    customer_id UUID PRIMARY KEY,
    username    TEXT        NOT NULL UNIQUE,
    account_id  TEXT        NOT NULL REFERENCES bank_accounts (account_id),
    pin_hash    TEXT        NOT NULL,
    CONSTRAINT customers_username_format CHECK (username ~ '^[a-z][a-z0-9_.-]{1,31}$')
);
COMMENT ON TABLE customers IS
    'Customers who can authenticate. The PIN is stored only as a salted scrypt hash, and not in any fixture or seed.';

CREATE TABLE authentication_challenges (
    challenge_id UUID PRIMARY KEY,
    customer_id  UUID        NOT NULL REFERENCES customers (customer_id),
    terms_digest TEXT        NOT NULL,
    code_digest  TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    attempts     SMALLINT    NOT NULL DEFAULT 0,
    status       TEXT        NOT NULL,
    CONSTRAINT authentication_challenges_status_valid CHECK (status IN ('PENDING', 'VERIFIED', 'LOCKED')),
    CONSTRAINT authentication_challenges_attempts_valid CHECK (attempts BETWEEN 0 AND 3),
    CONSTRAINT authentication_challenges_expiry_valid CHECK (expires_at > created_at)
);
COMMENT ON TABLE authentication_challenges IS
    'A one-time code sent to the customer, tied to the terms being approved. Only a keyed digest of the code is stored.';

CREATE TABLE authentication_proofs (
    proof_id     TEXT PRIMARY KEY,
    customer_id  UUID        NOT NULL REFERENCES customers (customer_id),
    terms_digest TEXT        NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    consumed     BOOLEAN     NOT NULL DEFAULT FALSE
);
COMMENT ON TABLE authentication_proofs IS
    'Evidence of strong authentication for one set of terms, good for one consent. proof_id is the digest of the bearer value.';

CREATE TABLE consents (
    consent_id          UUID PRIMARY KEY,
    customer_id         UUID        NOT NULL REFERENCES customers (customer_id),
    payer_account       TEXT        NOT NULL REFERENCES bank_accounts (account_id),
    payee_account       TEXT        NOT NULL REFERENCES bank_accounts (account_id),
    client_id           TEXT        NOT NULL,
    currency            CHAR(3)     NOT NULL,
    max_amount_minor    BIGINT      NOT NULL,
    scope               TEXT        NOT NULL,
    period_seconds      INTEGER,
    created_at          TIMESTAMPTZ NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ,
    used_at             TIMESTAMPTZ,
    CONSTRAINT consents_amount_positive CHECK (max_amount_minor > 0),
    CONSTRAINT consents_scope_valid CHECK (scope IN ('single-payment', 'recurring')),
    CONSTRAINT consents_period_matches_scope CHECK ((scope = 'recurring') = (period_seconds IS NOT NULL)),
    CONSTRAINT consents_expiry_valid CHECK (expires_at > created_at)
);
COMMENT ON TABLE consents IS
    'A grant created only after strong authentication: payer, payee, merchant, a cap, a scope and an expiry. '
    'Revoked and used are timestamps, never deletes.';
CREATE INDEX consents_client_id_idx ON consents (client_id);

COMMIT;
