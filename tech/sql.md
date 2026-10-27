# SQL

## What it is

SQL (Structured Query Language) is the language for talking to a relational database. A **table** has named columns and rows. SQL has two jobs here:

- **DDL** (data definition language): `CREATE TABLE`, `CREATE INDEX`, `CREATE FUNCTION`, `CREATE TRIGGER`. It describes the structure and the rules.
- **DML** (data manipulation language) and queries: `INSERT`, `UPDATE`, `DELETE`, `SELECT`. They change and read rows.

This repository uses the PostgreSQL dialect (see `postgresql.md` for the server itself).

## Why this repository uses it

The bank's rules (a balanced ledger, no overdraft, a consent bound to a customer who authenticated, a proof usable once, a payment recorded once) are written as constraints and triggers in `sql/001_schema.sql`, so they hold no matter what the Python code does. Other SQL files seed demo accounts, answer audit questions and show the stepwise migration path.

## Where it appears

| Path | Kind | Purpose |
| --- | --- | --- |
| `sql/001_schema.sql` | DDL | The whole schema: twelve tables, constraints, indexes, trigger functions. |
| `sql/002_seed.sql` | DML | Five accounts funded through balanced entries from a vault. **No credentials**: no customer, no merchant. |
| `sql/003_queries.sql` | Queries | Twelve audit queries, run by `make queries`. |
| `sql/reset.sql` | DDL | `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` |
| `sql/migrations/001..004_*.sql` | DDL | The same schema in four steps: accounts and ledger, customers and consent, merchant access, payments. |
| `src/.../infrastructure/postgres.py` | DML/queries | The statements the bank runs. |
| `tests/integration/test_schema_constraints.py` | DML | Statements that try to break the rules. |

## How it works underneath

The client sends SQL text. The server **parses** it, **plans** how to run it and **executes** the plan. Statements end with `;`. `--` starts a comment. Text values use single quotes (`'USD'`); a quote inside is doubled. Identifiers are lower-case by convention.

## Walkthrough of this repository's use

### Types

`TEXT`, `UUID`, `BIGINT` (64-bit integer), `INTEGER`, `CHAR(3)`, `BOOLEAN`, `TIMESTAMPTZ` (an instant with a time zone), `JSONB` (JSON stored in a binary form). Money is `BIGINT` minor units, never a float.

### Tables and constraints

```sql
CREATE TABLE consents (
    consent_id        UUID PRIMARY KEY,
    ...
    max_amount_minor  BIGINT      NOT NULL,
    scope             TEXT        NOT NULL,
    period_seconds    INTEGER,
    expires_at        TIMESTAMPTZ NOT NULL,
    CONSTRAINT consents_amount_positive CHECK (max_amount_minor > 0),
    CONSTRAINT consents_scope_valid CHECK (scope IN ('single-payment', 'recurring')),
    CONSTRAINT consents_period_matches_scope CHECK ((scope = 'recurring') = (period_seconds IS NOT NULL)),
    CONSTRAINT consents_expiry_valid CHECK (expires_at > created_at)
);
```

- `PRIMARY KEY`: unique, not null, identifies the row.
- `NOT NULL`: the column must have a value. A column without it (such as `period_seconds`) may be `NULL`, meaning "no value".
- `CONSTRAINT name CHECK (condition)`: a rule every row must satisfy. `IN (...)` is a list of allowed values. `(a) = (b)` between two true/false conditions is true when both are true or both false: here "recurring exactly when a period exists".
- Constraint names (`consents_period_matches_scope`) make error messages and tests precise.

```sql
CONSTRAINT customers_username_format CHECK (username ~ '^[a-z][a-z0-9_.-]{1,31}$'),
CONSTRAINT oauth_clients_id_format CHECK (client_id ~ '^[a-z][a-z0-9_.-]{1,63}$')
```

`~` is "matches this regular expression": a username starts with a lower-case letter and may then contain letters, digits and `_ . -`.

### References, uniqueness, JSON

```sql
client_id TEXT NOT NULL REFERENCES oauth_clients (client_id),
public_jwk JSONB NOT NULL,
CONSTRAINT journal_entries_once_per_reference UNIQUE (kind, reference)
```

- `REFERENCES`: the value must exist in the other table (a foreign key): a payment cannot name a merchant that is not registered.
- `JSONB NOT NULL`: the merchant's public key as a JSON object.
- `UNIQUE (kind, reference)`: a journal entry of a given kind and reference exists once; a payment posts exactly once because its payment id is the reference.

### Identity columns and indexes

`id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` lets the database number rows and refuse manual values. `CREATE INDEX ... ON postings (account_id)` builds a sorted side structure for fast lookups.

### Functions and triggers (PL/pgSQL)

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

- `$$ ... $$` quotes the function body. `DECLARE` introduces local variables; `:=` assigns; `SELECT ... INTO var` stores a query result in a variable.
- `NEW` is the row that fired the trigger. `COALESCE(x, 0)` replaces `NULL` (an account with no postings) by 0.
- `IS DISTINCT FROM` compares like `<>` but treats `NULL` as a value.
- `RAISE EXCEPTION '...% ...' , args USING ERRCODE = ...` aborts the transaction with a formatted message and a chosen error class.

### Inserting seed data

```sql
INSERT INTO bank_accounts (account_id, owner, currency, vault, balance_minor) VALUES
    ('acct-vault', 'Bank vault (funds the demo accounts)', 'USD', TRUE, -70000),
    ('acct-ada',   'Ada', 'USD', FALSE, 50000),
    ...
```

The vault holds -700.00 and the demo accounts +500.00 (Ada), +200.00 (Bob), 0 and 0, funded through balanced entries, so the ledger sums to zero from the first row. The seed contains no customer and no merchant: PINs and client secrets are credentials, and a credential does not belong in a fixture.

### Statements the program runs

```sql
INSERT INTO seen_proofs (jti, seen_at) VALUES (%s, %s) ON CONFLICT (jti) DO NOTHING RETURNING jti
```

An insert that does nothing if the row exists, and returns the id only when it inserted.

```sql
SELECT COALESCE(sum(amount_minor), 0)::bigint FROM payments WHERE consent_id = %s AND created_at >= %s AND currency = %s
```

How much a recurring consent has spent in the current window. `::bigint` casts the sum (PostgreSQL's `sum` of `BIGINT` is `NUMERIC`) back to an integer.

```sql
SELECT ... FROM authentication_challenges WHERE challenge_id = %s FOR UPDATE
```

`FOR UPDATE` locks the row, so two code attempts on one challenge take turns and the third wrong code reliably locks it.

### Audit queries

```sql
SELECT p.payment_id, p.amount_minor, c.max_amount_minor, p.created_at, c.expires_at, c.revoked_at
  FROM payments p JOIN consents c ON c.consent_id = p.consent_id
 WHERE p.amount_minor > c.max_amount_minor
    OR p.created_at >= c.expires_at
    OR (c.revoked_at IS NOT NULL AND p.created_at > c.revoked_at)
    OR p.client_id <> c.client_id;
```

Query 7 lists every payment that broke its consent (too large, after expiry, after revocation, by the wrong merchant). It must return **no rows**. `JOIN ... ON` pairs each payment with its consent. `OR` combines the four ways to be wrong.

```sql
count(*) FILTER (WHERE revoked_at IS NULL AND expires_at > now() AND used_at IS NULL) AS live
```

`count(*) FILTER (WHERE ...)` counts only some rows of a group (query 4: live, used, revoked and expired consents by scope). Query 8 is the strictest: for each recurring consent it finds the largest total spent in *any* window of the consent's period, using a nested `SELECT` and `make_interval(secs => ...)`, and that largest total must not exceed the consent's cap.

Queries 2, 3, 5, 6 and 7 are marked "must return no rows": they are checks that the schema's guarantees really hold.

### Migrations

`sql/migrations/001_accounts_and_ledger.sql` to `004_payments.sql` build the schema step by step. `make migrate` applies them to a scratch database, and an integration test compares the result to `001_schema.sql` table by table.

## Try it yourself

1. Load everything and look at the seed:

   ```bash
   make up
   docker compose exec -T postgres psql -U openbanking -d openbanking -c "select account_id, balance_minor from bank_accounts order by 1"
   ```

   Expected: `acct-ada 50000`, `acct-bob 20000`, `acct-other-shop 0`, `acct-shop 0`, `acct-vault -70000`.

2. Run the audit queries: `make queries`. Expected: `ledger_sum_minor` of `0` in section 1; sections 2, 3, 5, 6 and 7 print no rows.

3. Break a rule on purpose:

   ```bash
   docker compose exec -T postgres psql -U openbanking -d openbanking -c "insert into customers (customer_id, username, account_id, pin_hash) values (gen_random_uuid(), 'Not Valid', 'acct-ada', 'x')"
   ```

   Expected: `ERROR:  new row for relation "customers" violates check constraint "customers_username_format"`.

4. Apply the stepwise migrations to a scratch database: `make migrate`. Expected: `applying sql/migrations/001_accounts_and_ledger.sql` ... `004_payments.sql`, then a table list.

## Mistakes people make

- **Forgetting `NOT NULL`**, which lets `NULL` slip past `CHECK` conditions.
- **`=` with `NULL`.** Use `IS NULL` or `IS DISTINCT FROM`.
- **Building SQL from strings.** Use parameters.
- **Putting credentials in seed files.** They end up in git; generate them at setup time.
- **Checking a rule only in Python.** The database is the last line of defense.

## Check yourself

1. What does `ON CONFLICT (jti) DO NOTHING RETURNING jti` give when the row exists? *No row, so the caller knows it is a replay.*
2. Why does the seed contain no customers or merchants? *Their PINs and secrets are credentials, and fixtures live in git.*
3. What does `IS DISTINCT FROM` do differently from `<>`? *It treats `NULL` as a comparable value.*
4. What does `FOR UPDATE` do? *Locks the selected row until the transaction ends.*
5. What do the "must return no rows" queries check? *That the schema's guarantees hold in the data.*

## Further reading

- PostgreSQL 18, SQL language: https://www.postgresql.org/docs/18/sql.html
- Data definition: https://www.postgresql.org/docs/18/ddl.html
- Queries: https://www.postgresql.org/docs/18/queries.html
- PL/pgSQL: https://www.postgresql.org/docs/18/plpgsql.html
- `INSERT ... ON CONFLICT`: https://www.postgresql.org/docs/18/sql-insert.html
- `psql` meta-commands (`\echo`, `\dt`): https://www.postgresql.org/docs/18/app-psql.html
