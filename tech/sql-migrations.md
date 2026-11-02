# SQL walkthrough: the migrations, line by line

## What it is

This note reads the four files in `sql/migrations/`. A **migration** is a numbered SQL file that moves a database from one schema version to the next. Applying them in order gives the same schema as `sql/001_schema.sql`; an integration test compares the two.

## Why this repository uses it

A real database already holds data, so it cannot be dropped and rebuilt; it has to be changed step by step. The migrations follow the steps of the lesson: the ledger, then customers and consent, then merchant access, then payments.

## Where it appears

| File | What it is |
| --- | --- |
| `sql/migrations/001_accounts_and_ledger.sql` | Step 0: accounts and the ledger. |
| `sql/migrations/002_customers_and_consent.sql` | Steps 1 and 2: authentication and consent. |
| `sql/migrations/003_merchant_access.sql` | Step 3: merchants, tokens, seen proofs. |
| `sql/migrations/004_payments.sql` | Steps 4 to 6: payments and idempotency keys. |

## How it works underneath

Each migration is wrapped in `BEGIN;` ... `COMMIT;` so it applies completely or not at all. Later migrations create tables that refer to tables made earlier through foreign keys, so the order matters. The deferred triggers of migration 1 fire at the commit of whichever transaction inserts rows later.

## Walkthrough of this repository's use



### `sql/migrations/001_accounts_and_ledger.sql` (115 lines)

```sql
-- Step 0 of the lesson (its starting point): accounts and a ledger that cannot be unbalanced.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a credit to the account
-- and a negative amount is a debit. The database refuses an unbalanced entry, an edit to the ledger, a balance that
-- disagrees with the postings, an overdrawn customer account, and a second payment under one idempotency key.

BEGIN;
```

Migration 1 (step 0 of the lesson, its starting point): accounts and a ledger that cannot be unbalanced. A **migration** is a numbered SQL file that moves the database one step; applying them in order builds the same schema as `001_schema.sql` (a test proves it). 
The header comment explains the idea: money is a count of minor units in a `BIGINT` (a 64-bit whole number) with a currency, never a float; in `postings` a positive amount credits the account and a negative one debits it; and the database refuses an unbalanced entry, an edit to the ledger, a balance that disagrees with the postings, an overdrawn customer account, and a second payment under one idempotency key. Whatever the application does, these rules hold.

`BEGIN;` starts a **transaction**: the statements up to `COMMIT;` at the end are applied together or not at all. If any statement fails, nothing is left half-built.

```sql
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
```

The **accounts** table. `CREATE TABLE name (...)` defines a table; each line inside is a column with a **type** and rules. `account_id TEXT PRIMARY KEY`: text that is unique and never empty and identifies the row. `owner TEXT NOT NULL`: a name that must be given. `currency CHAR(3)`: exactly three characters. `vault BOOLEAN NOT NULL DEFAULT FALSE`: true for the one special account that funds the others. `balance_minor BIGINT NOT NULL DEFAULT 0`: the stored balance in minor units, starting at zero. Two named rules: `bank_accounts_currency_format` (`~` means "matches this regular expression": three capital letters) and `bank_accounts_not_overdrawn`, `vault OR balance_minor >= 0`, which means only the vault may go negative. `COMMENT ON TABLE ... IS '...'` stores a description inside the database.

```sql
CREATE TABLE journal_entries (
    id         UUID PRIMARY KEY,
    kind       TEXT        NOT NULL,
    reference  TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT journal_entries_kind_valid CHECK (kind IN ('FUNDING', 'PAYMENT')),
    CONSTRAINT journal_entries_once_per_reference UNIQUE (kind, reference)
);
COMMENT ON TABLE journal_entries IS 'One entry = one debit row and one equal credit row. A payment posts once.';
```

A **journal entry** groups the two postings of one transfer. `id UUID PRIMARY KEY` (a 128-bit identifier). `kind` is `FUNDING` (money from the vault) or `PAYMENT` (checked by `journal_entries_kind_valid`). `reference` says which instance: for a payment it is the payment id. `journal_entries_once_per_reference UNIQUE (kind, reference)` means a payment can be posted only once: the database itself refuses a second entry for the same payment.

```sql
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
```

A **posting** is one row of the ledger. `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` is a number the database assigns (1, 2, 3 ...) and refuses to accept from the program. `entry_id UUID NOT NULL REFERENCES journal_entries (id)` is a **foreign key**: the value must exist in `journal_entries`. `account_id` must be a known account. `amount_minor` is signed: here a positive amount credits the account and a negative amount debits it. `postings_amount_nonzero` forbids zero. Two **indexes** (sorted side structures that make lookups fast) follow, on the entry and on the account.

```sql
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
```

A **trigger function** written in PL/pgSQL (PostgreSQL's built-in programming language). `$$ ... $$` quotes the body. `DECLARE` introduces four variables. In the body, `SELECT count(*), sum(amount_minor), count(DISTINCT account_id), count(DISTINCT currency) INTO row_count, total, account_count, currency_count FROM postings WHERE entry_id = NEW.entry_id;` measures the entry: how many rows, what they sum to, how many different accounts and currencies. `NEW` is the row that fired the trigger. If the entry is not exactly two rows on two accounts in one currency summing to zero, `RAISE EXCEPTION '... % ...', args USING ERRCODE = 'check_violation'` stops the transaction with a message (each `%` is replaced by the next argument). `RETURN NULL` is the normal ending for an `AFTER` trigger.

```sql
CREATE CONSTRAINT TRIGGER postings_entry_balanced
    AFTER INSERT ON postings DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced();
```

The trigger itself. `CONSTRAINT TRIGGER ... AFTER INSERT ON postings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ...` fires after each inserted posting but, because it is *deferred*, only **at `COMMIT`**. That is needed because the two rows of an entry are inserted one after the other; the check is meaningful only when both are in.

```sql
CREATE FUNCTION assert_entry_has_postings() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT count(*) FROM postings WHERE entry_id = NEW.id) <> 2 THEN
        RAISE EXCEPTION 'journal entry % has no balanced pair of postings', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;
```

A second function guards the case the first cannot see: an entry with **no** postings never fires the first trigger. This one counts the postings of a new journal entry at `COMMIT` and raises an error unless there are exactly two.

```sql
CREATE CONSTRAINT TRIGGER journal_entries_have_postings
    AFTER INSERT ON journal_entries DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_has_postings();

-- The stored balance must equal the sum of the account's postings at COMMIT: money cannot move without a posting,
-- and a posting cannot be made without moving the balance.
```

The deferred trigger on `journal_entries` that runs the function above at commit.

```sql
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
```

The comment above it states the rule: the stored balance must equal the sum of the account's postings at `COMMIT`; money cannot move without a posting, and a posting cannot be made without moving the balance. The function declares three variables, sets `target_account := NEW.account_id` (`:=` assigns in PL/pgSQL), reads the account's stored balance, reads the sum of its postings (`COALESCE(sum(...), 0)` gives 0 for none) and raises an error if they differ. `IS DISTINCT FROM` is a comparison that treats two `NULL` values as equal and `NULL` against a value as different.

```sql
CREATE CONSTRAINT TRIGGER postings_balance_matches
    AFTER INSERT ON postings DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_balance_matches_postings();
```

A deferred trigger on `postings`: after a posting is inserted, check at commit that its account's balance matches.

```sql
CREATE CONSTRAINT TRIGGER bank_accounts_balance_matches
    AFTER INSERT OR UPDATE OF balance_minor ON bank_accounts DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_balance_matches_postings();
```

The same check from the other side: after an account is inserted or its `balance_minor` is updated (`AFTER INSERT OR UPDATE OF balance_minor`), check at commit that it matches its postings. Together the two triggers mean neither a posting without a balance change nor a balance change without a posting can commit.

```sql
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
```

The **append-only** rule. The function always raises an error saying `<table> is append-only; post a correcting entry instead` (`TG_TABLE_NAME` is the name of the table that fired it, `ERRCODE = 'object_not_in_prerequisite_state'` is the error class). The two `CREATE TRIGGER ... BEFORE UPDATE OR DELETE ON ... FOR EACH ROW EXECUTE FUNCTION forbid_mutation();` statements attach it to `postings` and `journal_entries`, so a posted row can never be edited or removed.

```sql
COMMIT;
```

`COMMIT;` applies the transaction. At this moment the deferred triggers run; if any entry or balance did not match the whole file or migration would fail.

### `sql/migrations/002_customers_and_consent.sql` (69 lines)

```sql
-- Steps 1 and 2 of the lesson: strong customer authentication, and consent as an object that exists only after it.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a credit to the account
-- and a negative amount is a debit. The database refuses an unbalanced entry, an edit to the ledger, a balance that
-- disagrees with the postings, an overdrawn customer account, and a second payment under one idempotency key.

BEGIN;
```

Migration 2 (steps 1 and 2 of the lesson): strong customer authentication, and consent as an object that exists only after it. The header repeats the money convention. `BEGIN;` opens a transaction.

```sql
CREATE TABLE customers (
    customer_id UUID PRIMARY KEY,
    username    TEXT        NOT NULL UNIQUE,
    account_id  TEXT        NOT NULL REFERENCES bank_accounts (account_id),
    pin_hash    TEXT        NOT NULL,
    CONSTRAINT customers_username_format CHECK (username ~ '^[a-z][a-z0-9_.-]{1,31}$')
);
COMMENT ON TABLE customers IS
    'Customers who can authenticate. The PIN is stored only as a salted scrypt hash, and not in any fixture or seed.';
```

Customers who can authenticate: an id, a unique username (`UNIQUE`), the account they own (a foreign key) and `pin_hash`. The check `username ~ '^[a-z][a-z0-9_.-]{1,31}$'` allows a lower-case letter followed by 1 to 31 lower-case letters, digits or `_ . -`. The comment says the PIN is stored only as a salted scrypt hash, and not in any fixture or seed.

```sql
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
```

A one-time code sent to the customer, tied to the terms being approved. `terms_digest` is the fingerprint of the terms; `code_digest` is the keyed digest of the code (never the code itself). `attempts SMALLINT` counts wrong codes (the check allows 0 to 3), `status` is `PENDING`, `VERIFIED` or `LOCKED`, and `expires_at > created_at` must hold.

```sql
CREATE TABLE authentication_proofs (
    proof_id     TEXT PRIMARY KEY,
    customer_id  UUID        NOT NULL REFERENCES customers (customer_id),
    terms_digest TEXT        NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    consumed     BOOLEAN     NOT NULL DEFAULT FALSE
);
COMMENT ON TABLE authentication_proofs IS
    'Evidence of strong authentication for one set of terms, good for one consent. proof_id is the digest of the bearer value.';
```

Evidence of strong authentication for one set of terms, good for one consent. `proof_id` is the digest of the bearer value (the table never holds a usable proof), `terms_digest` ties it to exactly the terms the customer saw, and `consumed` becomes true when a consent is created from it.

```sql
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
```

A grant created only after strong authentication: payer, payee, merchant (`client_id`), currency, the cap, the scope, an optional period, times, and `revoked_at` and `used_at` (empty until it happens; the comment: never deletes). The checks: the cap is positive; the scope is `single-payment` or `recurring`; `(scope = 'recurring') = (period_seconds IS NOT NULL)` says a recurring consent has a period and a single-payment one has none (comparing two true/false values with `=`); and it expires after it was created. An index on the merchant supports lookups.

```sql
COMMIT;
```

`COMMIT;` applies the transaction. At this moment the deferred triggers run; if any entry or balance did not match the whole file or migration would fail.

### `sql/migrations/003_merchant_access.sql` (36 lines)

```sql
-- Step 3 of the lesson: secure the merchant-to-bank API. Registered merchants, key-bound tokens, proofs already used.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a credit to the account
-- and a negative amount is a debit. The database refuses an unbalanced entry, an edit to the ledger, a balance that
-- disagrees with the postings, an overdrawn customer account, and a second payment under one idempotency key.

BEGIN;
```

Migration 3 (step 3 of the lesson): secure the merchant-to-bank API with registered merchants, key-bound tokens and the proofs already used. The header repeats the money convention. `BEGIN;` opens a transaction.

```sql
CREATE TABLE oauth_clients (
    client_id   TEXT PRIMARY KEY,
    name        TEXT        NOT NULL,
    secret_hash TEXT        NOT NULL,
    public_jwk  JSONB       NOT NULL,
    CONSTRAINT oauth_clients_id_format CHECK (client_id ~ '^[a-z][a-z0-9_.-]{1,63}$')
);
COMMENT ON TABLE oauth_clients IS
    'Registered merchants. The secret is stored only as a salted hash; the key is the public half of a pair the merchant holds.';
```

Registered merchants. `secret_hash` is the salted hash of the client secret. `public_jwk JSONB` is the public half of the merchant's key (`JSONB` is JSON stored in a binary form the database can query). The id must look like a lower-case name of 2 to 64 characters.

```sql
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
```

Short-lived tokens, stored as digests (`token_digest` is the primary key), each bound to the thumbprint of the merchant key it must be used with (`key_thumbprint`), for a merchant that exists (foreign key), expiring after creation.

```sql
CREATE TABLE seen_proofs (
    jti     TEXT PRIMARY KEY,
    seen_at TIMESTAMPTZ NOT NULL
);
COMMENT ON TABLE seen_proofs IS 'Proof-of-possession ids already used, so a captured proof cannot be replayed.';
```

Proof-of-possession ids (`jti`) already used, so a captured proof cannot be replayed. The primary key makes a repeat impossible to insert.

```sql
COMMIT;
```

`COMMIT;` applies the transaction. At this moment the deferred triggers run; if any entry or balance did not match the whole file or migration would fail.

### `sql/migrations/004_payments.sql` (37 lines)

```sql
-- Steps 4 to 6 of the lesson: initiate against consent, once only, with a consistent state on failure.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a credit to the account
-- and a negative amount is a debit. The database refuses an unbalanced entry, an edit to the ledger, a balance that
-- disagrees with the postings, an overdrawn customer account, and a second payment under one idempotency key.

BEGIN;
```

Migration 4 (steps 4 to 6 of the lesson): initiate against a consent, once only, with a consistent state on failure. The header repeats the money convention. `BEGIN;` opens a transaction.

```sql
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
```

Payments made under a consent. It references the consent, the merchant and both accounts, and stores the amount and currency. The comment: each has exactly one ledger entry (`journal_entries.reference`). An index on `(consent_id, created_at)` supports the period-cap query.

```sql
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
```

The memory of the idempotency scheme, scoped to the merchant: `PRIMARY KEY (client_id, key)` means two merchants may use the same key text but one merchant cannot use a key twice. `request_fingerprint` is the hash of the request, `response_status SMALLINT` and `response_body JSONB` are the stored answer, and `expires_at > created_at` is checked. The comment: it is written in the same transaction as the payment it answers.

```sql
COMMIT;
```

`COMMIT;` applies the transaction. At this moment the deferred triggers run; if any entry or balance did not match the whole file or migration would fail.

## Try it yourself

1. Apply them to a scratch database and list the tables:

   ```bash
   make up
   make migrate
   ```

   Expected: a list that includes all twelve tables.

2. Compare the schemas: run the integration test `test_the_migrations_build_the_same_schema_as_001_schema` with `.venv/bin/python -m pytest tests/integration -k migrations`.

## Mistakes people make

- **Editing an old migration after it has been applied.** Add a new one instead.
- **Creating a table before the table it refers to.** The order of the files is the order of the dependencies.
- **Letting a migration leave the schema different from the direct file.** The comparison test catches it.

## Check yourself

1. Which migration adds the payments table? *The fourth.*
2. What makes a migration all-or-nothing? *`BEGIN;` and `COMMIT;` around it.*
3. What does the comparison test prove? *The four migrations give the same schema as `001_schema.sql`.*
4. Why does migration 4 come after migration 3? *Payments refer to merchants (`oauth_clients`), created in migration 3.*
5. Where do the ledger rules come from in a migrated database? *Migration 1 creates the same triggers.*

## Further reading

- PostgreSQL 18 `ALTER TABLE`: https://www.postgresql.org/docs/18/sql-altertable.html
- PostgreSQL 18 transactions: https://www.postgresql.org/docs/18/tutorial-transactions.html
