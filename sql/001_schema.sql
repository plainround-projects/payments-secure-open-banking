-- 001_schema.sql: the complete schema (DDL). Run by `make db`; the stepwise path to the same schema is in migrations/.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a credit to the account
-- and a negative amount is a debit. The database refuses an unbalanced entry, an edit to the ledger, a balance that
-- disagrees with the postings, an overdrawn customer account, and a second payment under one idempotency key.

BEGIN;

CREATE TABLE bank_accounts (
    account_id    TEXT PRIMARY KEY,
    owner         TEXT        NOT NULL,
    currency      CHAR(3)     NOT NULL,
    vault         BOOLEAN     NOT NULL DEFAULT FALSE,
    balance_minor BIGINT      NOT NULL DEFAULT 0,
    CONSTRAINT bank_accounts_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT bank_accounts_not_overdrawn CHECK (vault OR balance_minor >= 0)
);
COMMENT ON TABLE bank_accounts IS
    'Customer and merchant accounts, and one vault account that funds them. Only the vault may go negative.';

CREATE TABLE journal_entries (
    id         UUID PRIMARY KEY,
    kind       TEXT        NOT NULL,
    reference  TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT journal_entries_kind_valid CHECK (kind IN ('FUNDING', 'PAYMENT')),
    CONSTRAINT journal_entries_once_per_reference UNIQUE (kind, reference)
);
COMMENT ON TABLE journal_entries IS 'One entry = one debit row and one equal credit row. A payment posts once.';

CREATE TABLE postings (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id     UUID        NOT NULL REFERENCES journal_entries (id),
    account_id   TEXT        NOT NULL REFERENCES bank_accounts (account_id),
    amount_minor BIGINT      NOT NULL,
    currency     CHAR(3)     NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    CONSTRAINT postings_amount_nonzero CHECK (amount_minor <> 0)
);
COMMENT ON TABLE postings IS 'One row per debit (negative) or credit (positive). Append-only; the sum of the table is zero.';
CREATE INDEX postings_entry_id_idx ON postings (entry_id);
CREATE INDEX postings_account_id_idx ON postings (account_id);

CREATE FUNCTION assert_entry_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    row_count      INTEGER;
    total          BIGINT;
    account_count  INTEGER;
    currency_count INTEGER;
BEGIN
    SELECT count(*), sum(amount_minor), count(DISTINCT account_id), count(DISTINCT currency)
      INTO row_count, total, account_count, currency_count
      FROM postings WHERE entry_id = NEW.entry_id;
    IF row_count <> 2 OR total <> 0 OR account_count <> 2 OR currency_count <> 1 THEN
        RAISE EXCEPTION 'journal entry % is not a balanced pair (rows %, sum %, accounts %, currencies %)',
            NEW.entry_id, row_count, total, account_count, currency_count
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER postings_entry_balanced
    AFTER INSERT ON postings DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced();

CREATE FUNCTION assert_entry_has_postings() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT count(*) FROM postings WHERE entry_id = NEW.id) <> 2 THEN
        RAISE EXCEPTION 'journal entry % has no balanced pair of postings', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER journal_entries_have_postings
    AFTER INSERT ON journal_entries DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_has_postings();

-- The stored balance must equal the sum of the account's postings at COMMIT: money cannot move without a posting,
-- and a posting cannot be made without moving the balance.
CREATE FUNCTION assert_balance_matches_postings() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_account TEXT;
    stored         BIGINT;
    posted         BIGINT;
BEGIN
    target_account := NEW.account_id;
    SELECT balance_minor INTO stored FROM bank_accounts WHERE account_id = target_account;
    SELECT COALESCE(sum(amount_minor), 0) INTO posted FROM postings WHERE account_id = target_account;
    IF stored IS DISTINCT FROM posted THEN
        RAISE EXCEPTION 'account % balance % does not equal its postings %', target_account, stored, posted
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER postings_balance_matches
    AFTER INSERT ON postings DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_balance_matches_postings();
CREATE CONSTRAINT TRIGGER bank_accounts_balance_matches
    AFTER INSERT OR UPDATE OF balance_minor ON bank_accounts DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_balance_matches_postings();

CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; post a correcting entry instead', TG_TABLE_NAME
        USING ERRCODE = 'object_not_in_prerequisite_state';
END
$$;
CREATE TRIGGER postings_append_only BEFORE UPDATE OR DELETE ON postings
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER journal_entries_append_only BEFORE UPDATE OR DELETE ON journal_entries
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

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
