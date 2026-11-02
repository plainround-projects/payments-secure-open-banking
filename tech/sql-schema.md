# SQL walkthrough: the schema, line by line

## What it is

This note reads the two SQL files that build and empty the database: `sql/001_schema.sql` and `sql/reset.sql`. SQL is the language for talking to a relational database; these files use the **DDL** part (data definition language: `CREATE TABLE`, `CREATE INDEX`, `CREATE FUNCTION`, `CREATE TRIGGER`), which describes structure and rules. It is the line-by-line companion of `sql.md` and `postgresql.md`.

## Why this repository uses it

The bank's ledger must stay correct even if the program has a bug. The schema puts the rules in the database, where they cannot be bypassed: an entry that is not a balanced pair cannot commit, a stored balance that disagrees with its postings cannot commit, a posted row cannot be edited or deleted, a customer account cannot be overdrawn, a payment can be posted only once, and an idempotency key can answer only one request.

## Where it appears

| File | What it is |
| --- | --- |
| `sql/001_schema.sql` | The whole schema. |
| `sql/reset.sql` | Drops everything so `make db` starts empty. |

## How it works underneath

PostgreSQL checks every constraint when a row is written; a violation raises an error and the whole transaction is rolled back. A *deferred* constraint trigger waits until `COMMIT` to run, so several statements can be applied before the combined result is checked. A *trigger* is a function that PostgreSQL calls automatically on `INSERT`, `UPDATE` or `DELETE`. A *foreign key* makes one table's value refer to a row of another. An *index* is a sorted copy of some columns that makes lookups fast. A *unique* constraint is enforced with a unique index.

## Walkthrough of this repository's use



### `sql/001_schema.sql` (230 lines)

```sql
-- 001_schema.sql: the complete schema (DDL). Run by `make db`; the stepwise path to the same schema is in migrations/.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a credit to the account
-- and a negative amount is a debit. The database refuses an unbalanced entry, an edit to the ledger, a balance that
-- disagrees with the postings, an overdrawn customer account, and a second payment under one idempotency key.

BEGIN;
```

The header comment explains the idea: money is a count of minor units in a `BIGINT` (a 64-bit whole number) with a currency, never a float; in `postings` a positive amount credits the account and a negative one debits it; and the database refuses an unbalanced entry, an edit to the ledger, a balance that disagrees with the postings, an overdrawn customer account, and a second payment under one idempotency key. Whatever the application does, these rules hold.

`BEGIN;` starts a **transaction**: the statements up to `COMMIT;` at the end are applied together or not at all. If any statement fails, nothing is left half-built.

The comment also says this file is the complete schema, and that the stepwise path to the same schema is in `migrations/`.

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

### `sql/reset.sql` (4 lines)

```sql
-- reset.sql: drop everything so `make db` starts from an empty database. The ledger is append-only, so a reset is
-- a schema drop, not a DELETE. Local development only.
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
```

Two statements that empty the database. `DROP SCHEMA public CASCADE;` deletes the schema called `public` (the default namespace that holds all the tables) together with everything inside it: `CASCADE` means "and everything that depends on it". `CREATE SCHEMA public;` makes a new empty one. A schema drop is used instead of `DELETE` because the ledger tables refuse updates and deletes by design. The comment says it is for local development only.

## Try it yourself

1. Build the schema and the seed:

   ```bash
   make up
   ```

   Expected: PostgreSQL starts and the schema and seed are loaded.

2. List the tables:

   ```bash
   docker compose exec -T postgres psql -U openbanking -d openbanking -c '\dt'
   ```

   Expected: `access_tokens`, `authentication_challenges`, `authentication_proofs`, `bank_accounts`, `consents`, `customers`, `idempotency_keys`, `journal_entries`, `oauth_clients`, `payments`, `postings`, `seen_proofs`.

3. Try to break a rule (the transaction is rolled back, nothing is changed):

   ```bash
   docker compose exec -T postgres psql -U openbanking -d openbanking -c "UPDATE postings SET amount_minor = 1 WHERE id = 1"
   ```

   Expected: `ERROR:  postings is append-only; post a correcting entry instead`.

## Mistakes people make

- **Moving money by updating a balance only.** The deferred triggers refuse a balance that does not equal the sum of the postings.
- **Editing a posted row to fix a mistake.** The triggers forbid it; post a correcting entry.
- **Checking balance after every row.** The two rows of an entry arrive one after the other; the check is deferred to commit.
- **Storing money in a float column.** Amounts are `BIGINT` minor units.
- **Putting a credential in the seed.** No customer and no merchant is seeded.

## Check yourself

1. Why are two triggers needed for balances? *One checks from the postings' side and one from the accounts' side, so neither a posting without a balance change nor a balance change without a posting can commit.*
2. Which account may be negative? *Only the vault.*
3. What does `UNIQUE (kind, reference)` prevent? *Posting the same payment twice.*
4. Why is the idempotency key's primary key two columns? *Keys are scoped to the merchant.*
5. What does `IS DISTINCT FROM` do? *Compares two values treating two NULLs as equal.*

## Further reading

- PostgreSQL 18 `CREATE TABLE`: https://www.postgresql.org/docs/18/sql-createtable.html
- PostgreSQL 18 `CREATE TRIGGER`: https://www.postgresql.org/docs/18/sql-createtrigger.html
- PostgreSQL 18 constraints: https://www.postgresql.org/docs/18/ddl-constraints.html
- PostgreSQL 18 PL/pgSQL: https://www.postgresql.org/docs/18/plpgsql.html
