# SQL walkthrough: the seed data and the queries, line by line

## What it is

This note reads `sql/002_seed.sql`, which loads five accounts funded from a vault, and `sql/003_queries.sql`, the twelve queries of the lesson. These use **DML** (data manipulation: `INSERT`) and **queries** (`SELECT`). It is the companion of `sql.md` and `psql.md`.

## Why this repository uses it

The scenario needs accounts with money in them before anything can be paid, and the funding must itself obey the ledger rules, so it comes from a vault through balanced entries. The queries are the lesson's way of looking at the books directly; several of them are invariants that must return nothing.

## Where it appears

| File | What it is |
| --- | --- |
| `sql/002_seed.sql` | The accounts and their funding. |
| `sql/003_queries.sql` | The twelve lesson queries. |

## How it works underneath

An `INSERT` adds rows; a `SELECT` reads them. `GROUP BY` collapses rows into groups and aggregate functions (`count`, `sum`, `max`) summarize each group; `FILTER (WHERE ...)` limits an aggregate to some rows; `HAVING` filters groups after grouping. A `JOIN` combines rows of two tables on a condition; `LEFT JOIN` keeps rows with no match. `psql` runs a file statement by statement and understands its own backslash commands such as `\echo`.

## Walkthrough of this repository's use



### `sql/002_seed.sql` (30 lines)

```sql
-- 002_seed.sql: the data the lesson's examples use (DML). Every row satisfies the constraints of 001_schema.sql.
--
-- The lesson's two accounts, 'acct-ada' with 500.00 and 'acct-shop' with 0.00, plus a second customer account
-- ('acct-bob' with 200.00) and a second merchant account ('acct-other-shop'), so the refusals that need "another
-- customer's consent" and "another merchant" have somebody to refuse. The money comes from a vault account through
-- balanced entries, so the ledger sums to zero from the first row.
--
-- No customer and no merchant is seeded: a customer's PIN and a merchant's key and secret are credentials, and a
-- credential does not belong in a fixture. `make register` (or `make run`) creates them with random values.

BEGIN;
```

The header says what the data is: the lesson's two accounts, `acct-ada` with 500.00 and `acct-shop` with 0.00, plus a second customer account (`acct-bob` with 200.00) and a second merchant account (`acct-other-shop`), so the refusals that need "another customer's consent" and "another merchant" have somebody to refuse. The money comes from a **vault** account through balanced entries, so the ledger sums to zero from the first row. The comment also states a security rule: **no customer and no merchant is seeded**, because a PIN, a key and a secret are credentials and a credential does not belong in a fixture; `make register` (or `make run`) creates them with random values. This is **DML** (data manipulation language: `INSERT`), as opposed to the table definitions in `001_schema.sql`. `BEGIN;` opens a transaction.

```sql
INSERT INTO bank_accounts (account_id, owner, currency, vault, balance_minor) VALUES
    ('acct-vault',      'Bank vault (funds the demo accounts)', 'USD', TRUE,  -70000),
    ('acct-ada',        'Ada',                                  'USD', FALSE,  50000),
    ('acct-bob',        'Bob',                                  'USD', FALSE,  20000),
    ('acct-shop',       'Shop',                                 'USD', FALSE,      0),
    ('acct-other-shop', 'Other Shop',                           'USD', FALSE,      0);
```

Five accounts: the vault (`vault` true, balance -70000, i.e. minus 700.00, because it has paid out the other accounts' money), Ada (50000 = 500.00), Bob (20000 = 200.00), the shop and the other shop (both 0). The columns are the id, a display name, the currency, the vault flag and the balance in minor units. The vault is the only account allowed a negative balance. The statement ends at `;`.

```sql
INSERT INTO journal_entries (id, kind, reference, created_at) VALUES
    ('dddddddd-0000-4000-8000-000000000001', 'FUNDING', 'seed-acct-ada', '2026-10-20T08:00:00Z'),
    ('dddddddd-0000-4000-8000-000000000002', 'FUNDING', 'seed-acct-bob', '2026-10-20T08:00:00Z');
```

Two funding entries (kind `FUNDING`), one for each customer account, with fixed ids, the reference `seed-acct-ada` / `seed-acct-bob` and a time (`Z` means UTC).

```sql
INSERT INTO postings (entry_id, account_id, amount_minor, currency, created_at) VALUES
    ('dddddddd-0000-4000-8000-000000000001', 'acct-vault', -50000, 'USD', '2026-10-20T08:00:00Z'),
    ('dddddddd-0000-4000-8000-000000000001', 'acct-ada',    50000, 'USD', '2026-10-20T08:00:00Z'),
    ('dddddddd-0000-4000-8000-000000000002', 'acct-vault', -20000, 'USD', '2026-10-20T08:00:00Z'),
    ('dddddddd-0000-4000-8000-000000000002', 'acct-bob',    20000, 'USD', '2026-10-20T08:00:00Z');
```

Four postings, two per entry: for Ada, -50000 on the vault and +50000 on Ada; for Bob, -20000 on the vault and +20000 on Bob. Each pair sums to zero, and each account's balance (set above) equals the sum of its postings, which the deferred triggers check at commit: the vault's -70000 is the sum of -50000 and -20000.

```sql
COMMIT;
```

`COMMIT;` applies the transaction. At this moment the deferred triggers check that every entry is a balanced pair and every balance matches its postings.

### `sql/003_queries.sql` (69 lines)

```sql
-- 003_queries.sql: the queries the lesson runs. Run with `make queries`.
```

The queries the lesson runs, with `make queries`. Lines that start with `\echo` are not SQL: they are commands of the `psql` client, which prints the text after them, so the output is labeled.

```sql
\echo '== 1. Balances, and the ledger invariant: the sum of every posting (must be 0)'
SELECT account_id, owner, balance_minor, to_char(balance_minor / 100.0, 'FM999999990.00') AS balance
  FROM bank_accounts ORDER BY account_id;
SELECT COALESCE(sum(amount_minor), 0) AS ledger_sum_minor FROM postings;
```

Query 1 has two statements. The first lists every account with its stored balance and the balance as a decimal text: `to_char(balance_minor / 100.0, 'FM999999990.00')` divides by 100.0 (a decimal, so no integer division) and formats it with two decimals (`FM` removes padding). The second returns the grand total of every posting, which must be 0 (`COALESCE(x, 0)` replaces `NULL` by 0).

```sql
\echo '== 2. Accounts whose stored balance disagrees with their postings (must return no rows)'
SELECT a.account_id, a.balance_minor, COALESCE(sum(p.amount_minor), 0) AS posted_minor
  FROM bank_accounts a LEFT JOIN postings p ON p.account_id = a.account_id
 GROUP BY a.account_id, a.balance_minor
HAVING a.balance_minor <> COALESCE(sum(p.amount_minor), 0);
```

Query 2 looks for accounts whose stored balance disagrees with their postings. `LEFT JOIN` keeps accounts with no postings; `GROUP BY` makes one row per account; `HAVING` keeps only groups where the stored balance differs from the sum (`<>` means "not equal"). It must return no rows.

```sql
\echo '== 3. Entries that are not a balanced pair (must return no rows; the schema makes them impossible)'
SELECT entry_id, count(*) AS rows, sum(amount_minor) AS total
  FROM postings GROUP BY entry_id HAVING count(*) <> 2 OR sum(amount_minor) <> 0;
```

Query 3: entries that are not balanced pairs. `GROUP BY entry_id HAVING count(*) <> 2 OR sum(amount_minor) <> 0` keeps entries that break the rule. The schema makes them impossible, so no rows.

```sql
\echo '== 4. Consents: how many are live, used, revoked or expired, by scope'
SELECT scope,
       count(*) FILTER (WHERE revoked_at IS NULL AND expires_at > now() AND used_at IS NULL) AS live,
       count(*) FILTER (WHERE used_at IS NOT NULL) AS used,
       count(*) FILTER (WHERE revoked_at IS NOT NULL) AS revoked,
       count(*) FILTER (WHERE revoked_at IS NULL AND expires_at <= now()) AS expired
  FROM consents GROUP BY scope ORDER BY scope;
```

Query 4: how many consents are live, used, revoked or expired, by scope. `count(*) FILTER (WHERE ...)` counts only the rows meeting the condition. Live means not revoked, not expired and not used; `now()` is the current time.

```sql
\echo '== 5. Single-payment consents with more than one payment (must return no rows)'
SELECT c.consent_id, count(p.payment_id) AS payments
  FROM consents c JOIN payments p ON p.consent_id = c.consent_id
 WHERE c.scope = 'single-payment' GROUP BY c.consent_id HAVING count(p.payment_id) > 1;
```

Query 5: single-payment consents with more than one payment. A join of consents and payments, restricted to the single-payment scope, grouped by consent and keeping groups with more than one payment. No rows expected.

```sql
\echo '== 6. Payments without exactly one ledger entry, and entries without a payment (must return no rows)'
SELECT p.payment_id, count(e.id) AS entries
  FROM payments p LEFT JOIN journal_entries e ON e.kind = 'PAYMENT' AND e.reference = p.payment_id::text
 GROUP BY p.payment_id HAVING count(e.id) <> 1
UNION ALL
SELECT e.reference::uuid, 0 FROM journal_entries e
 WHERE e.kind = 'PAYMENT' AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.payment_id::text = e.reference);
```

Query 6 joins the payments to their ledger entries by reference (`p.payment_id::text` casts the id to text) and reports payments that do not have exactly one entry, and (after `UNION ALL`, which stacks two result sets) entries that have no payment (`NOT EXISTS (SELECT 1 ...)` is true when the inner query finds nothing). No rows expected.

```sql
\echo '== 7. Payments over their consent: above the cap, or after expiry or revocation (must return no rows)'
SELECT p.payment_id, p.amount_minor, c.max_amount_minor, p.created_at, c.expires_at, c.revoked_at
  FROM payments p JOIN consents c ON c.consent_id = p.consent_id
 WHERE p.amount_minor > c.max_amount_minor
    OR p.created_at >= c.expires_at
    OR (c.revoked_at IS NOT NULL AND p.created_at > c.revoked_at)
    OR p.client_id <> c.client_id;
```

Query 7 reports payments that should not exist: above the consent's cap, made at or after its expiry, made after it was revoked, or made by a merchant other than the consent's. No rows expected.

```sql
\echo '== 8. Recurring consents: the most spent in any window of the consent period, against the cap (must not exceed)'
SELECT c.consent_id, c.max_amount_minor AS cap_minor,
       (SELECT COALESCE(max(window_total), 0) FROM (
           SELECT (SELECT sum(q.amount_minor) FROM payments q
                    WHERE q.consent_id = c.consent_id AND q.created_at >= p.created_at
                      AND q.created_at < p.created_at + make_interval(secs => c.period_seconds)) AS window_total
             FROM payments p WHERE p.consent_id = c.consent_id) windows) AS most_in_a_window_minor
  FROM consents c WHERE c.scope = 'recurring' ORDER BY c.consent_id;
```

Query 8, for recurring consents: the most spent in any window of the consent's period, against the cap. For each payment, the inner query sums the payments starting at that payment and lasting one period (`make_interval(secs => c.period_seconds)` builds the length of time); the outer query takes the largest such sum. It must not exceed the cap.

```sql
\echo '== 9. Payments per merchant'
SELECT client_id, count(*) AS payments, sum(amount_minor) AS total_minor FROM payments GROUP BY client_id ORDER BY client_id;
```

Query 9: payments per merchant, with count and total.

```sql
\echo '== 10. Idempotency keys per merchant: stored answers, and how many have expired'
SELECT client_id, count(*) AS keys, count(*) FILTER (WHERE expires_at <= now()) AS expired
  FROM idempotency_keys GROUP BY client_id ORDER BY client_id;
```

Query 10: idempotency keys per merchant, and how many have expired.

```sql
\echo '== 11. Authentication challenges by status (a locked challenge means three wrong codes)'
SELECT status, count(*) AS challenges, max(attempts) AS most_attempts FROM authentication_challenges GROUP BY status ORDER BY status;
```

Query 11: authentication challenges by status and the most attempts seen in each (a locked challenge means three wrong codes).

```sql
\echo '== 12. Proofs of authentication: used once each; a proof that was consumed, and how many consents exist'
SELECT (SELECT count(*) FROM authentication_proofs WHERE consumed) AS consumed_proofs,
       (SELECT count(*) FROM authentication_proofs WHERE NOT consumed) AS unused_proofs,
       (SELECT count(*) FROM consents) AS consents;
```

Query 12: three counts in one row, each from its own subquery: proofs consumed, proofs unused, and consents. Since a proof is good for one consent, consumed proofs and consents should match in a clean run.

## Try it yourself

1. Run the queries:

   ```bash
   make up
   make queries
   ```

   Expected: twelve labeled sections; the ledger sum is 0 and the queries marked "must return no rows" return none.

2. See the funding in the ledger: `docker compose exec -T postgres psql -U openbanking -d openbanking -c "SELECT account_id, amount_minor FROM postings ORDER BY id"`. Expected: four rows (-50000, 50000, -20000, 20000).

## Mistakes people make

- **Seeding credentials.** A PIN or a key does not belong in a fixture.
- **Forgetting `LEFT JOIN`.** An inner join would drop accounts with no postings.
- **Treating `NULL` as zero.** `sum` of nothing is `NULL`; `COALESCE` supplies the 0.

## Check yourself

1. Why does the vault have a negative balance? *It paid out the money in the other accounts; the vault is the only account allowed to go negative.*
2. What does `HAVING` do that `WHERE` cannot? *Filters groups after aggregation.*
3. What does `\echo` do? *It is a psql command that prints a label; it is not SQL.*
4. What does `UNION ALL` do? *Stacks the rows of two queries.*
5. What makes the seed's last line important? *`COMMIT` runs the deferred checks.*

## Further reading

- PostgreSQL 18 `SELECT`: https://www.postgresql.org/docs/18/sql-select.html
- PostgreSQL 18 aggregate `FILTER`: https://www.postgresql.org/docs/18/sql-expressions.html#SYNTAX-AGGREGATES
- psql: https://www.postgresql.org/docs/18/app-psql.html
