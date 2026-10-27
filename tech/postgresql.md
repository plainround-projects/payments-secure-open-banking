# PostgreSQL

## What it is

PostgreSQL (often "Postgres") is a **relational database server**: a long-running program that stores data in tables, answers queries written in SQL, and promises that a group of changes either all happens or none of it does. Clients (the bank's Python program and the `psql` command) connect to it over the network, by default on port 5432. This repository pins **major version 18** (`image: postgres:18`). Only the **bank** uses the database; the merchant keeps its small state in memory.

## Why this repository uses it

The bank's rules about money and consent must hold even if the Python code has a bug or two programs race. PostgreSQL enforces them itself: an unbalanced ledger entry, an edit to a posted row, an account whose stored balance disagrees with its postings, an overdrawn customer account, a PIN-less consent, and a second payment under one idempotency key are all refused by the database. It also provides the **locks** that make concurrent payments take turns.

## Where it appears

| Path | Role |
| --- | --- |
| `docker-compose.yml` | Runs the `postgres:18` container on host port 54613, with a health check. |
| `sql/*.sql` | The schema, seed accounts, queries and migrations (see `sql.md`). |
| `src/open_banking/infrastructure/postgres.py` | The only Python file that talks to it. |
| `Makefile` | `up`, `db`, `queries`, `migrate`, `down` drive it through `psql`. |
| `tests/integration/` | Tests against the real server, including a race of 20 concurrent payments. |
| `.github/workflows/ci.yml` | Starts `postgres:18` as a service container. |

## How it works underneath

- **Server process and data directory.** The container runs `postgres`, which keeps tables as files and a **write-ahead log** (WAL): every change is first written to the log, then to the table files. After a crash the log is replayed.
- **Transactions.** `BEGIN` ... `COMMIT` groups statements. Until `COMMIT`, other connections do not see the changes. `ROLLBACK` discards them.
- **Constraints.** `NOT NULL`, `CHECK`, `UNIQUE`, `PRIMARY KEY` and `FOREIGN KEY` are tested on every write.
- **Triggers.** A trigger runs a function on `INSERT`, `UPDATE` or `DELETE`. A **constraint trigger** can be *deferred* to `COMMIT`.
- **Locks.** `SELECT ... FOR UPDATE` locks the selected rows until the transaction ends; other transactions that want the same rows wait. An **advisory lock** is a lock on a number the program picks.
- **Deadlock.** If transaction A waits for a lock B holds, while B waits for one A holds, neither can proceed; PostgreSQL detects it and aborts one. Locking things in the same order everywhere avoids it.

## Walkthrough of this repository's use

### The container

```yaml
postgres:
  image: postgres:18
  command: ["postgres", "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"]
  ports:
    - "${POSTGRES_PORT:-54613}:5432"
```

- The three `-c` flags switch off disk-safety steps so tests run fast on a busy machine. The official documentation warns that `fsync=off` can corrupt the database after an operating-system or hardware crash, and `synchronous_commit=off` can lose the last few committed transactions. That is acceptable only because this database is **disposable** (rebuilt by `make db`). Never copy these flags to data you need.
- `5432` inside the container is published as `54613` on your computer.
- The health check is `pg_isready -h 127.0.0.1 ...`: `-h 127.0.0.1` forces a TCP check, because during first start the image runs a temporary server that listens only on a local socket; a socket check would report ready too early.

### Money cannot be created or lost

```sql
CREATE TABLE bank_accounts (
    account_id    TEXT PRIMARY KEY,
    vault         BOOLEAN     NOT NULL DEFAULT FALSE,
    balance_minor BIGINT      NOT NULL DEFAULT 0,
    CONSTRAINT bank_accounts_not_overdrawn CHECK (vault OR balance_minor >= 0)
);
```

Only the special `acct-vault` account (which funds the demo accounts) may go negative; every customer or merchant account must stay at zero or above, whatever the application does.

```sql
CREATE CONSTRAINT TRIGGER postings_balance_matches
    AFTER INSERT ON postings DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_balance_matches_postings();
CREATE CONSTRAINT TRIGGER bank_accounts_balance_matches
    AFTER INSERT OR UPDATE OF balance_minor ON bank_accounts DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_balance_matches_postings();
```

At `COMMIT`, the function compares each touched account's stored balance with `sum(amount_minor)` over its postings and raises an error if they differ (`IS DISTINCT FROM` treats `NULL` safely). So money cannot move without a posting, and a posting cannot be made without moving the balance. The two triggers cover both directions: inserting a posting, and updating a balance. Deferral lets the program insert the postings and update the balances in any order inside the transaction.

```sql
CREATE CONSTRAINT TRIGGER postings_entry_balanced
    AFTER INSERT ON postings DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced();
```

Every journal entry must be exactly two postings, on two accounts, in one currency, summing to zero. A second trigger rejects an entry with no postings. A `BEFORE UPDATE OR DELETE` trigger (`forbid_mutation`) makes the ledger append-only.

### Consent and authentication rules in the schema

```sql
CONSTRAINT authentication_challenges_attempts_valid CHECK (attempts BETWEEN 0 AND 3),
CONSTRAINT consents_period_matches_scope CHECK ((scope = 'recurring') = (period_seconds IS NOT NULL)),
CONSTRAINT consents_expiry_valid CHECK (expires_at > created_at)
```

A challenge cannot record a fourth attempt; a recurring consent must have a period and a single-payment consent must not; every expiry is after creation. `=` between two true/false expressions means "both true or both false".

### Replay protection by a primary key

```sql
CREATE TABLE seen_proofs (jti TEXT PRIMARY KEY, seen_at TIMESTAMPTZ NOT NULL);
```

```sql
INSERT INTO seen_proofs (jti, seen_at) VALUES (%s, %s) ON CONFLICT (jti) DO NOTHING RETURNING jti
```

`ON CONFLICT (jti) DO NOTHING` skips the insert when the id exists, and `RETURNING jti` returns a row only when the insert happened. No row means "seen before", so the proof is a replay. The database settles two simultaneous uses of the same proof.

### Locks used by a payment

```sql
SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))      -- the (merchant, key) lock
SELECT ... FROM consents WHERE consent_id = %s FOR UPDATE   -- the consent
SELECT ... FROM bank_accounts WHERE account_id = %s FOR UPDATE   -- both accounts
```

A payment takes locks in a **fixed order**: first the idempotency key (so duplicates queue up), then the consent (so two payments cannot both think they are within its cap), then both accounts **sorted by id** (`sorted({payer, payee})` in Python), so two payments in opposite directions cannot wait for each other in a circle. `pg_advisory_xact_lock(bigint)` is an advisory lock released automatically at the end of the transaction; `hashtextextended(text, seed)` is a built-in hash that turns the text into a number (an internal helper the manual does not list). The advisory key is the merchant id plus the idempotency key, joined with a separator character, so **different merchants never block each other** even if they pick the same key text.

### Rolling back refusals

A refusal raises an exception inside the unit of work; the connection pool's context manager rolls the transaction back, so a refused payment leaves **nothing** behind: no ledger rows, no stored key.

## Try it yourself

1. Start the database and load the schema and seed:

   ```bash
   make up
   ```

   Expected: a container named `payments-secure-open-banking-postgres-1` becomes healthy; no errors.

2. Open a shell into it:

   ```bash
   docker compose exec postgres psql -U openbanking -d openbanking
   ```

   Then `\dt` (twelve tables) and `select sum(amount_minor) from postings;` (expected `0`).

3. Try to break the ledger (inside `psql`):

   ```sql
   update postings set amount_minor = 1 where id = 1;
   ```

   Expected: `ERROR:  postings is append-only; post a correcting entry instead`.

4. Try to overdraw a customer:

   ```sql
   update bank_accounts set balance_minor = -5 where account_id = 'acct-ada';
   ```

   Expected: `ERROR:  new row for relation "bank_accounts" violates check constraint "bank_accounts_not_overdrawn"`.

5. Try to change a balance without a posting:

   ```sql
   begin; update bank_accounts set balance_minor = balance_minor + 1 where account_id = 'acct-ada'; commit;
   ```

   Expected: at `COMMIT`, `ERROR:  account acct-ada balance 50001 does not equal its postings 50000`.

6. Run the lesson queries: `make queries`. Expected: sections `== 1. ...` to `== 12. ...`, and the "must return no rows" queries empty.

7. Stop and delete everything: `make down`.

## Mistakes people make

- **Using `FLOAT` for money.** Use integers in minor units.
- **Checking invariants only in the application.** Another program or a race can bypass it; the database cannot be bypassed.
- **Locking in different orders.** That is how deadlocks happen; here accounts are locked in sorted order.
- **Checking "is the consent within its cap?" without a lock.** Two concurrent payments could both pass. The consent row lock serializes them.
- **Copying `fsync=off` to production.** Disposable data only.

## Check yourself

1. Why is `postings_balance_matches` deferred? *Postings and the balance update happen in separate statements; the check is meaningful only when all are in.*
2. Which account may have a negative balance? *Only the vault.*
3. In what order does a payment take its locks? *Idempotency key, consent, then both accounts sorted by id.*
4. How does the database detect a replayed proof? *`INSERT ... ON CONFLICT DO NOTHING RETURNING` returns no row when the `jti` exists.*
5. What happens to a refused payment's database changes? *The transaction rolls back; nothing is stored.*

## Further reading

- PostgreSQL 18 documentation: https://www.postgresql.org/docs/18/
- Constraints: https://www.postgresql.org/docs/18/ddl-constraints.html
- CREATE TRIGGER (constraint triggers): https://www.postgresql.org/docs/18/sql-createtrigger.html
- Explicit locking, row locks and advisory locks: https://www.postgresql.org/docs/18/explicit-locking.html
- `INSERT ... ON CONFLICT`: https://www.postgresql.org/docs/18/sql-insert.html
- WAL settings (`fsync`, `synchronous_commit`): https://www.postgresql.org/docs/18/runtime-config-wal.html
