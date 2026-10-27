-- 003_queries.sql: the queries the lesson runs. Run with `make queries`.

\echo '== 1. Balances, and the ledger invariant: the sum of every posting (must be 0)'
SELECT account_id, owner, balance_minor, to_char(balance_minor / 100.0, 'FM999999990.00') AS balance
  FROM bank_accounts ORDER BY account_id;
SELECT COALESCE(sum(amount_minor), 0) AS ledger_sum_minor FROM postings;

\echo '== 2. Accounts whose stored balance disagrees with their postings (must return no rows)'
SELECT a.account_id, a.balance_minor, COALESCE(sum(p.amount_minor), 0) AS posted_minor
  FROM bank_accounts a LEFT JOIN postings p ON p.account_id = a.account_id
 GROUP BY a.account_id, a.balance_minor
HAVING a.balance_minor <> COALESCE(sum(p.amount_minor), 0);

\echo '== 3. Entries that are not a balanced pair (must return no rows; the schema makes them impossible)'
SELECT entry_id, count(*) AS rows, sum(amount_minor) AS total
  FROM postings GROUP BY entry_id HAVING count(*) <> 2 OR sum(amount_minor) <> 0;

\echo '== 4. Consents: how many are live, used, revoked or expired, by scope'
SELECT scope,
       count(*) FILTER (WHERE revoked_at IS NULL AND expires_at > now() AND used_at IS NULL) AS live,
       count(*) FILTER (WHERE used_at IS NOT NULL) AS used,
       count(*) FILTER (WHERE revoked_at IS NOT NULL) AS revoked,
       count(*) FILTER (WHERE revoked_at IS NULL AND expires_at <= now()) AS expired
  FROM consents GROUP BY scope ORDER BY scope;

\echo '== 5. Single-payment consents with more than one payment (must return no rows)'
SELECT c.consent_id, count(p.payment_id) AS payments
  FROM consents c JOIN payments p ON p.consent_id = c.consent_id
 WHERE c.scope = 'single-payment' GROUP BY c.consent_id HAVING count(p.payment_id) > 1;

\echo '== 6. Payments without exactly one ledger entry, and entries without a payment (must return no rows)'
SELECT p.payment_id, count(e.id) AS entries
  FROM payments p LEFT JOIN journal_entries e ON e.kind = 'PAYMENT' AND e.reference = p.payment_id::text
 GROUP BY p.payment_id HAVING count(e.id) <> 1
UNION ALL
SELECT e.reference::uuid, 0 FROM journal_entries e
 WHERE e.kind = 'PAYMENT' AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.payment_id::text = e.reference);

\echo '== 7. Payments over their consent: above the cap, or after expiry or revocation (must return no rows)'
SELECT p.payment_id, p.amount_minor, c.max_amount_minor, p.created_at, c.expires_at, c.revoked_at
  FROM payments p JOIN consents c ON c.consent_id = p.consent_id
 WHERE p.amount_minor > c.max_amount_minor
    OR p.created_at >= c.expires_at
    OR (c.revoked_at IS NOT NULL AND p.created_at > c.revoked_at)
    OR p.client_id <> c.client_id;

\echo '== 8. Recurring consents: the most spent in any window of the consent period, against the cap (must not exceed)'
SELECT c.consent_id, c.max_amount_minor AS cap_minor,
       (SELECT COALESCE(max(window_total), 0) FROM (
           SELECT (SELECT sum(q.amount_minor) FROM payments q
                    WHERE q.consent_id = c.consent_id AND q.created_at >= p.created_at
                      AND q.created_at < p.created_at + make_interval(secs => c.period_seconds)) AS window_total
             FROM payments p WHERE p.consent_id = c.consent_id) windows) AS most_in_a_window_minor
  FROM consents c WHERE c.scope = 'recurring' ORDER BY c.consent_id;

\echo '== 9. Payments per merchant'
SELECT client_id, count(*) AS payments, sum(amount_minor) AS total_minor FROM payments GROUP BY client_id ORDER BY client_id;

\echo '== 10. Idempotency keys per merchant: stored answers, and how many have expired'
SELECT client_id, count(*) AS keys, count(*) FILTER (WHERE expires_at <= now()) AS expired
  FROM idempotency_keys GROUP BY client_id ORDER BY client_id;

\echo '== 11. Authentication challenges by status (a locked challenge means three wrong codes)'
SELECT status, count(*) AS challenges, max(attempts) AS most_attempts FROM authentication_challenges GROUP BY status ORDER BY status;

\echo '== 12. Proofs of authentication: used once each; a proof that was consumed, and how many consents exist'
SELECT (SELECT count(*) FROM authentication_proofs WHERE consumed) AS consumed_proofs,
       (SELECT count(*) FROM authentication_proofs WHERE NOT consumed) AS unused_proofs,
       (SELECT count(*) FROM consents) AS consents;
