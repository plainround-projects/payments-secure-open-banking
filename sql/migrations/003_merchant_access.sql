-- Step 3 of the lesson: secure the merchant-to-bank API. Registered merchants, key-bound tokens, proofs already used.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a credit to the account
-- and a negative amount is a debit. The database refuses an unbalanced entry, an edit to the ledger, a balance that
-- disagrees with the postings, an overdrawn customer account, and a second payment under one idempotency key.

BEGIN;

CREATE TABLE oauth_clients (
    client_id   TEXT PRIMARY KEY,
    name        TEXT        NOT NULL,
    secret_hash TEXT        NOT NULL,
    public_jwk  JSONB       NOT NULL,
    CONSTRAINT oauth_clients_id_format CHECK (client_id ~ '^[a-z][a-z0-9_.-]{1,63}$')
);
COMMENT ON TABLE oauth_clients IS
    'Registered merchants. The secret is stored only as a salted hash; the key is the public half of a pair the merchant holds.';

CREATE TABLE access_tokens (
    token_digest   TEXT PRIMARY KEY,
    client_id      TEXT        NOT NULL REFERENCES oauth_clients (client_id),
    key_thumbprint TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    CONSTRAINT access_tokens_expiry_valid CHECK (expires_at > created_at)
);
COMMENT ON TABLE access_tokens IS
    'Short-lived tokens, stored as digests, each bound to the thumbprint of the merchant key it must be used with.';

CREATE TABLE seen_proofs (
    jti     TEXT PRIMARY KEY,
    seen_at TIMESTAMPTZ NOT NULL
);
COMMENT ON TABLE seen_proofs IS 'Proof-of-possession ids already used, so a captured proof cannot be replayed.';

COMMIT;
