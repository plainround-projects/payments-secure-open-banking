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

INSERT INTO bank_accounts (account_id, owner, currency, vault, balance_minor) VALUES
    ('acct-vault',      'Bank vault (funds the demo accounts)', 'USD', TRUE,  -70000),
    ('acct-ada',        'Ada',                                  'USD', FALSE,  50000),
    ('acct-bob',        'Bob',                                  'USD', FALSE,  20000),
    ('acct-shop',       'Shop',                                 'USD', FALSE,      0),
    ('acct-other-shop', 'Other Shop',                           'USD', FALSE,      0);

INSERT INTO journal_entries (id, kind, reference, created_at) VALUES
    ('dddddddd-0000-4000-8000-000000000001', 'FUNDING', 'seed-acct-ada', '2026-10-20T08:00:00Z'),
    ('dddddddd-0000-4000-8000-000000000002', 'FUNDING', 'seed-acct-bob', '2026-10-20T08:00:00Z');

INSERT INTO postings (entry_id, account_id, amount_minor, currency, created_at) VALUES
    ('dddddddd-0000-4000-8000-000000000001', 'acct-vault', -50000, 'USD', '2026-10-20T08:00:00Z'),
    ('dddddddd-0000-4000-8000-000000000001', 'acct-ada',    50000, 'USD', '2026-10-20T08:00:00Z'),
    ('dddddddd-0000-4000-8000-000000000002', 'acct-vault', -20000, 'USD', '2026-10-20T08:00:00Z'),
    ('dddddddd-0000-4000-8000-000000000002', 'acct-bob',    20000, 'USD', '2026-10-20T08:00:00Z');

COMMIT;
