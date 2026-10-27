# psycopg and psycopg_pool

## What it is

`psycopg` (version 3) is the Python **driver** for PostgreSQL: a library that opens a network connection to the database, sends it SQL text and reads back rows. `psycopg_pool` is its companion package that keeps a **pool** of open connections ready, so each request does not pay the cost of connecting. The `[binary]` extra installs a prebuilt C library (`psycopg-binary`) so nothing needs compiling.

## Why this repository uses it

All of the bank's state lives in PostgreSQL: accounts and the ledger, customers, challenges and proofs, consents, merchants, tokens, seen proofs, payments and idempotency keys. A payment must write its ledger entry, update two balances, store the payment and store the idempotency key's answer **in one transaction**, and concurrent payments must take turns on the consent and on the accounts. psycopg gives explicit transactions, row locks and advisory locks, and its pool serves the bank's concurrent requests.

## Where it appears

| Path | Role |
| --- | --- |
| `src/open_banking/infrastructure/postgres.py` | All database code (498 lines): connection string, pool, the repositories (accounts, ledger, challenges and proofs, consents, clients, tokens, payments, idempotency keys), the unit of work, the health probe. |
| `src/open_banking/infrastructure/settings.py` | The values for the connection (`PostgresSettings`). |
| `tests/integration/*` | Open plain connections to load the schema and to test it, and run the concurrency race. |
| `requirements.lock` | `psycopg==3.3.6`, `psycopg-binary==3.3.6`, `psycopg-pool==3.3.3`. |

## How it works underneath

1. `psycopg.connect(...)` opens a TCP connection to the server (host and port) and logs in with a user and password. The wire protocol is PostgreSQL's own.
2. By default a connection starts a **transaction** the first time you run a statement; nothing is permanent until `commit()`. `rollback()` throws the changes away.
3. `conn.execute(sql, params)` sends the SQL text and the parameters **separately**. The server fills the placeholders `%s` itself, so a parameter can never turn into SQL (the defense against SQL injection).
4. Results come back as rows; `fetchone()` gives one row (a tuple) or `None`, `fetchall()` gives a list, and iterating the cursor streams rows one at a time.
5. The pool opens up to `max_size` connections, lends one to `pool.connection()`, and takes it back afterwards. If all are lent, the caller waits up to a `timeout`.
6. In **pipeline mode** the client sends several statements without waiting for each answer, and reads the answers afterwards, saving network round trips.

## Walkthrough of this repository's use

### The connection string

```python
return make_conninfo(
    host=settings.host, port=settings.port, dbname=settings.database,
    user=settings.user, password=settings.password,
    connect_timeout=settings.connect_timeout_seconds,
    application_name="open-banking",
    options="-c timezone=UTC",
)
```

`make_conninfo` builds the text PostgreSQL's client library (**libpq**) understands, quoting values correctly. `application_name` shows up in the server's `pg_stat_activity`. `options="-c timezone=UTC"` sets the session time zone for every connection, so "the day of a timestamp" is always a UTC calendar day.

### Opening the pool

```python
pool = ConnectionPool(conninfo(settings), min_size=1, max_size=settings.pool_max_size, open=False, timeout=15.0)
pool.open(wait=True, timeout=float(settings.connect_timeout_seconds) * 6)
```

`min_size=1`: keep at least one connection. `max_size`: from `DB_POOL_MAX_SIZE`, default 20. `open=False` creates the pool object without starting its background workers; `pool.open(wait=True, timeout=...)` then starts them and **waits** until a working connection exists, or fails after 30 seconds (six times the connect timeout, which gives a database that is still starting some time). A missing database becomes a loud startup error instead of a hang on the first request. `timeout=15.0` is how long a request waits for a free connection.

### One unit of work = one transaction

```python
@contextmanager
def __call__(self) -> Iterator[UnitOfWork]:
    with self._pool.connection() as connection:
        yield PostgresUnitOfWork(PostgresPayments(connection), PostgresLedger(connection, self._ids), ...)
```

The documented behavior of `pool.connection()`: leaving the block normally **commits** the open transaction; leaving it with an exception **rolls it back**; then the connection returns to the pool. So everything done through the repositories inside one `with` is all-or-nothing. Initiating a payment runs in one unit of work: the idempotency claim, the checks, the ledger entry, the balances, the payment row and the stored answer commit together or not at all.

### Parameters and placeholders

```python
self._connection.execute(
    "INSERT INTO access_tokens (token_digest, client_id, key_thumbprint, created_at, expires_at) VALUES (%s, %s, %s, %s, %s)",
    (token.token_digest, token.client_id, token.key_thumbprint, token.created_at, token.expires_at),
)
```

`%s` marks a value to fill; the tuple holds them in order. Python values are converted for you: `datetime` to `timestamptz`, `UUID` to `uuid`, `None` to `NULL`. The only SQL built with an f-string is a fixed column list that is a constant in the source, never user input.

### JSON and "insert only if new"

```python
Jsonb(dict(client.public_jwk))
```

`Jsonb` marks a dict to be stored as PostgreSQL `jsonb` (the merchant's public key; the stored answer of an idempotency key). Reading a `jsonb` column back gives a Python dict.

```python
row = self._connection.execute(
    "INSERT INTO seen_proofs (jti, seen_at) VALUES (%s, %s) ON CONFLICT (jti) DO NOTHING RETURNING jti",
    (jti, at),
).fetchone()
return row is not None
```

`fetchone()` returns a row or `None`. With `ON CONFLICT DO NOTHING RETURNING`, a row comes back only when the insert really happened, so `row is not None` means "first time this proof is seen"; `None` means a replay.

### Locks

```python
"SELECT ... FROM consents WHERE consent_id = %s FOR UPDATE"
"SELECT ... FROM bank_accounts WHERE account_id = %s FOR UPDATE"
"SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
```

`FOR UPDATE` locks the row until the transaction ends; concurrent payments under one consent, or on one account, take turns. The advisory lock is taken on the text `f"{client_id}\x1f{key.value}"`: the merchant id, an unprintable separator character (`\x1f`), and the idempotency key, so the same key text from two merchants never collides. A payment takes its locks in a fixed order, key then consent then both accounts sorted by id (see `postgresql.md`). All release automatically at the end of the transaction.

### Pipeline mode

```python
with self._connection.pipeline():
    self._connection.execute("INSERT INTO journal_entries ...", (...))
    for posting in transfer.postings():
        self._connection.execute("INSERT INTO postings ...", (...))
        self._connection.execute("UPDATE bank_accounts SET balance_minor = balance_minor + %s WHERE account_id = %s", (...))
```

A transfer is five statements: one journal entry, two postings and two balance updates. Inside `with connection.pipeline():` they are sent together and the answers read at the end of the block, so they cost about one network round trip instead of five. The deferred triggers still run at `COMMIT`, so the entry, the postings and the balances are checked together.

### Health probe

```python
with self._pool.connection(timeout=2.0) as connection:
    connection.execute("SELECT 1")
```

Borrow a connection (waiting at most 2 seconds) and run the smallest query. If it raises, `/health` answers 503.

### In the tests

`errors.CheckViolation` (from `psycopg.errors`) is what a failed `CHECK` constraint raises; `pytest.raises(errors.CheckViolation)` proves the database refuses unbalanced entries. `wait_until_ready` in `tests/conftest.py` retries `psycopg.connect` while the container is starting, catching `psycopg.OperationalError`.

## Try it yourself

1. With the database up (`make up`) and the bank running (`make serve-bank`), see the pooled connections:

   ```bash
   docker compose exec -T postgres psql -U openbanking -d openbanking -c "select application_name, state from pg_stat_activity where application_name = 'open-banking'"
   ```

   Expected: one or more rows named `open-banking` (none if the server is not running).

2. Run one query from Python:

   ```bash
   set -a; . ./.env; set +a; .venv/bin/python -c "
   import psycopg
   from open_banking.infrastructure import postgres
   from open_banking.infrastructure.settings import Settings
   s = Settings.from_env()
   with psycopg.connect(postgres.conninfo(s.postgres)) as c:
       print(c.execute('select count(*) from bank_accounts').fetchone(), c.execute('show timezone').fetchone())"
   ```

   Expected after `make db`: `(5,) ('UTC',)` (the five seeded accounts, and the session zone set by the connection option). `set -a; . ./.env; set +a` loads the variables from `.env` into the shell.

3. Prove rollback: run `make test-integration` and read the `CheckViolation` the schema tests expect.

## Mistakes people make

- **Building SQL with f-strings and user values.** Always pass values as parameters.
- **Forgetting to commit.** Outside a `with` block the work is lost on close. The unit of work commits on a normal exit.
- **Holding a pooled connection too long.** The pool is limited; each unit of work here is short.
- **Using one connection from many threads.** Take one per unit of work from the pool.
- **Trusting `CHAR(3)` to be unpadded.** The code calls `.strip()` on currencies read back.
- **Ignoring the result of `INSERT ... ON CONFLICT DO NOTHING`.** Whether a row came back is the answer to "was this new?".

## Check yourself

1. What does leaving `with pool.connection()` do after an exception? *Rolls the transaction back and returns the connection to the pool.*
2. Why are parameters sent separately from the SQL? *So values can never be interpreted as SQL.*
3. What does `FOR UPDATE` do? *Locks the selected rows until the transaction ends.*
4. What does pipeline mode save? *Network round trips: statements are sent together and the answers read later.*
5. What does `ON CONFLICT DO NOTHING RETURNING jti` return when the proof id already exists? *No row, so the caller knows the proof is a replay.*

## Further reading

- psycopg 3.3 documentation: https://www.psycopg.org/psycopg3/docs/
- Connection pools: https://www.psycopg.org/psycopg3/docs/advanced/pool.html
- Transactions: https://www.psycopg.org/psycopg3/docs/basic/transactions.html
- Pipeline mode: https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html
- Adapting types (lists as arrays, dates): https://www.psycopg.org/psycopg3/docs/basic/adapt.html
