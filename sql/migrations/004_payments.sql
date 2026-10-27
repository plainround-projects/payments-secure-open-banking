-- Steps 4 to 6 of the lesson: initiate against consent, once only, with a consistent state on failure.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a credit to the account
-- and a negative amount is a debit. The database refuses an unbalanced entry, an edit to the ledger, a balance that
-- disagrees with the postings, an overdrawn customer account, and a second payment under one idempotency key.

BEGIN;

CREATE TABLE payments (
    payment_id    UUID PRIMARY KEY,
    consent_id    UUID        NOT NULL REFERENCES consents (consent_id),
    client_id     TEXT        NOT NULL REFERENCES oauth_clients (client_id),
    payer_account TEXT        NOT NULL REFERENCES bank_accounts (account_id),
    payee_account TEXT        NOT NULL REFERENCES bank_accounts (account_id),
    amount_minor  BIGINT      NOT NULL,
    currency      CHAR(3)     NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL,
    CONSTRAINT payments_amount_positive CHECK (amount_minor > 0)
);
COMMENT ON TABLE payments IS 'Payments made under a consent. Each has exactly one ledger entry (journal_entries.reference).';
CREATE INDEX payments_consent_id_idx ON payments (consent_id, created_at);

CREATE TABLE idempotency_keys (
    client_id           TEXT        NOT NULL REFERENCES oauth_clients (client_id),
    key                 TEXT        NOT NULL,
    request_fingerprint TEXT        NOT NULL,
    response_status     SMALLINT    NOT NULL,
    response_body       JSONB       NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (client_id, key),
    CONSTRAINT idempotency_keys_expiry_after_creation CHECK (expires_at > created_at)
);
COMMENT ON TABLE idempotency_keys IS
    'Scoped to the merchant: two merchants may use the same key. Written in the same transaction as the payment it answers.';

COMMIT;
