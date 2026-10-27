"""The database refuses what the domain never produces: an unbalanced entry, a balance that disagrees, an overdraft."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from psycopg import errors

from tests.conftest import SQL

pytestmark = pytest.mark.integration

NOW = "2026-10-27T10:00:00Z"


def entry(connection: psycopg.Connection[Any], reference: str | None = None) -> str:
    entry_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO journal_entries (id, kind, reference, created_at) VALUES (%s, 'PAYMENT', %s, %s)",
        (entry_id, reference or str(uuid.uuid4()), NOW),
    )
    return entry_id


def post(
    connection: psycopg.Connection[Any], entry_id: str, account: str, amount: int, *, move_balance: bool = True
) -> None:
    connection.execute(
        "INSERT INTO postings (entry_id, account_id, amount_minor, currency, created_at) "
        "VALUES (%s, %s, %s, 'USD', %s)",
        (entry_id, account, amount, NOW),
    )
    if move_balance:
        connection.execute(
            "UPDATE bank_accounts SET balance_minor = balance_minor + %s WHERE account_id = %s", (amount, account)
        )


def check_deferred_constraints_now(connection: psycopg.Connection[Any]) -> None:
    """Fire the checks that would run at COMMIT, then roll back so the test leaves nothing behind."""
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.rollback()


def test_a_balanced_transfer_passes_the_commit_check(connection):
    entry_id = entry(connection)
    post(connection, entry_id, "acct-ada", -1000)
    post(connection, entry_id, "acct-shop", 1000)
    check_deferred_constraints_now(connection)


@pytest.mark.parametrize(
    "rows",
    [
        [("acct-ada", -1000)],
        [("acct-ada", -1000), ("acct-shop", 900)],
        [("acct-ada", -1000), ("acct-ada", 1000)],
        [("acct-ada", -1000), ("acct-shop", 500), ("acct-bob", 500)],
        [],
    ],
)
def test_an_entry_that_is_not_a_balanced_pair_cannot_commit(connection, rows):
    entry_id = entry(connection)
    for account, amount in rows:
        post(connection, entry_id, account, amount)
    with pytest.raises(errors.CheckViolation):
        connection.commit()


def test_a_balance_that_moved_without_a_posting_cannot_commit(connection):
    connection.execute("UPDATE bank_accounts SET balance_minor = balance_minor + 100 WHERE account_id = 'acct-shop'")
    with pytest.raises(errors.CheckViolation, match="does not equal its postings"):
        connection.commit()


def test_a_posting_without_the_balance_moving_cannot_commit(connection):
    entry_id = entry(connection)
    post(connection, entry_id, "acct-ada", -1000, move_balance=False)
    post(connection, entry_id, "acct-shop", 1000, move_balance=False)
    with pytest.raises(errors.CheckViolation, match="does not equal its postings"):
        connection.commit()


def test_a_customer_account_cannot_be_overdrawn_but_the_vault_may_be_negative(connection):
    entry_id = entry(connection)
    with pytest.raises(errors.CheckViolation, match="bank_accounts_not_overdrawn"):
        post(connection, entry_id, "acct-ada", -50_001)
    connection.rollback()
    assert connection.execute("SELECT balance_minor FROM bank_accounts WHERE account_id = 'acct-vault'").fetchone() == (
        -70_000,
    )


def test_the_ledger_cannot_be_edited_or_deleted(connection):
    for statement in (
        "UPDATE postings SET amount_minor = amount_minor + 1",
        "DELETE FROM postings",
        "DELETE FROM journal_entries",
        "UPDATE journal_entries SET reference = 'x'",
    ):
        with pytest.raises(errors.ObjectNotInPrerequisiteState, match="append-only"):
            connection.execute(statement)
        connection.rollback()


def test_a_posting_of_zero_and_a_repeated_reference_are_refused(connection):
    entry_id = entry(connection, "ref-1")
    with pytest.raises(errors.CheckViolation, match="postings_amount_nonzero"):
        post(connection, entry_id, "acct-ada", 0)
    connection.rollback()
    entry(connection, "ref-2")
    with pytest.raises(errors.UniqueViolation, match="journal_entries_once_per_reference"):
        entry(connection, "ref-2")


def test_idempotency_keys_are_scoped_to_the_merchant(connection):
    for client in ("merchant-1", "merchant-2"):
        connection.execute(
            "INSERT INTO oauth_clients (client_id, name, secret_hash, public_jwk) VALUES (%s, %s, 'h', '{}')",
            (client, client),
        )
    insert = (
        "INSERT INTO idempotency_keys (client_id, key, request_fingerprint, response_status, response_body, "
        "created_at, expires_at) VALUES (%s, 'same-key', 'f', 201, '{}', now(), now() + interval '1 day')"
    )
    connection.execute(insert, ("merchant-1",))
    connection.execute(insert, ("merchant-2",))  # another merchant may use the same key
    with pytest.raises(errors.UniqueViolation):
        connection.execute(insert, ("merchant-1",))
    connection.rollback()


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        ("UPDATE bank_accounts SET currency = 'usd' WHERE account_id = 'acct-ada'", "bank_accounts_currency_format"),
        (
            "INSERT INTO customers (customer_id, username, account_id, pin_hash) "
            "VALUES (gen_random_uuid(), 'Ada!', 'acct-ada', 'h')",
            "customers_username_format",
        ),
        (
            "INSERT INTO oauth_clients (client_id, name, secret_hash, public_jwk) VALUES ('Bad Id', 'x', 'h', '{}')",
            "oauth_clients_id_format",
        ),
    ],
)
def test_malformed_identifiers_and_currencies_are_refused(connection, sql, constraint):
    with pytest.raises(errors.CheckViolation, match=constraint):
        connection.execute(sql)


def test_consents_must_be_consistent(connection):
    connection.execute(
        "INSERT INTO customers (customer_id, username, account_id, pin_hash) VALUES "
        "('11111111-0000-4000-8000-000000000001', 'ada', 'acct-ada', 'h')"
    )
    connection.execute(
        "INSERT INTO oauth_clients (client_id, name, secret_hash, public_jwk) VALUES ('merchant-1', 'm', 'h', '{}')"
    )
    base = (
        "INSERT INTO consents (consent_id, customer_id, payer_account, payee_account, client_id, currency, "
        "max_amount_minor, scope, period_seconds, created_at, expires_at) VALUES (gen_random_uuid(), "
        "'11111111-0000-4000-8000-000000000001', 'acct-ada', 'acct-shop', 'merchant-1', 'USD', %s, %s, %s, now(), "
        "now() + %s)"
    )
    cases = [
        ((0, "single-payment", None, "10 minutes"), "consents_amount_positive"),
        ((100, "forever", None, "10 minutes"), "consents_scope_valid"),
        ((100, "recurring", None, "10 minutes"), "consents_period_matches_scope"),
        ((100, "single-payment", 86400, "10 minutes"), "consents_period_matches_scope"),
        ((100, "single-payment", None, "-1 minutes"), "consents_expiry_valid"),
    ]
    for params, constraint in cases:
        with pytest.raises(errors.CheckViolation, match=constraint):
            connection.execute(base.replace("now() + %s", "now() + %s::interval"), params)
        connection.rollback()
        connection.execute(
            "INSERT INTO customers (customer_id, username, account_id, pin_hash) VALUES "
            "('11111111-0000-4000-8000-000000000001', 'ada', 'acct-ada', 'h') ON CONFLICT DO NOTHING"
        )
        connection.execute(
            "INSERT INTO oauth_clients (client_id, name, secret_hash, public_jwk) "
            "VALUES ('merchant-1', 'm', 'h', '{}') ON CONFLICT DO NOTHING"
        )
    connection.rollback()


def test_the_seed_funds_the_demo_accounts_through_balanced_entries_and_holds_no_credentials(connection):
    balances = dict(connection.execute("SELECT account_id, balance_minor FROM bank_accounts").fetchall())
    assert balances == {
        "acct-vault": -70_000,
        "acct-ada": 50_000,
        "acct-bob": 20_000,
        "acct-shop": 0,
        "acct-other-shop": 0,
    }
    assert connection.execute("SELECT COALESCE(sum(amount_minor), 0) FROM postings").fetchone() == (0,)
    for table in ("customers", "oauth_clients", "access_tokens", "authentication_proofs"):
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_the_queries_file_runs_and_its_invariant_queries_return_nothing(connection):
    script = "\n".join(line for line in (SQL / "003_queries.sql").read_text().splitlines() if not line.startswith("\\"))
    cursor = connection.execute(script)
    results = []
    while True:
        results.append(cursor.fetchall() if cursor.description else None)
        if not cursor.nextset():
            break
    assert len(results) == 13
    assert results[1] == [(0,)]  # the ledger sum
    for index in (
        2,
        3,
        5,
        6,
        7,
    ):  # balance mismatches, unbalanced entries, single-payment overuse, ledger gaps, rule breaks
        assert results[index] == [], index
    connection.rollback()


def catalogs(settings: Any) -> tuple[list[Any], list[Any]]:
    catalog_sql = """
        SELECT 'column', c.table_name || '.' || c.column_name,
               c.data_type || ' ' || c.is_nullable || ' ' || COALESCE(c.column_default, '')
          FROM information_schema.columns c WHERE c.table_schema = 'public'
        UNION ALL
        SELECT 'constraint', conrelid::regclass::text || '.' || conname, pg_get_constraintdef(oid) FROM pg_constraint
         WHERE connamespace = 'public'::regnamespace
        UNION ALL
        SELECT 'index', tablename || '.' || indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'
        UNION ALL
        SELECT 'trigger', tgrelid::regclass::text || '.' || tgname,
               regexp_replace(pg_get_triggerdef(oid), ' ON .*? ', ' ON ')
          FROM pg_trigger WHERE NOT tgisinternal
        UNION ALL
        SELECT 'function', proname, prosrc FROM pg_proc WHERE pronamespace = 'public'::regnamespace
        UNION ALL
        SELECT 'comment', c.relname, obj_description(c.oid) FROM pg_class c
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
        ORDER BY 1, 2
    """
    from open_banking.infrastructure.postgres import conninfo

    found = []
    suffix = uuid.uuid4().hex[:8]
    with psycopg.connect(conninfo(settings.postgres), autocommit=True) as admin:
        for label, scripts in (
            ("direct", [SQL / "001_schema.sql"]),
            ("migrated", sorted((SQL / "migrations").glob("*.sql"))),
        ):
            name = f"scratch_{label}_{suffix}"
            admin.execute(f'CREATE DATABASE "{name}"')
            try:
                info = conninfo(settings.postgres).replace(f"dbname={settings.postgres.database}", f"dbname={name}")
                with psycopg.connect(info, autocommit=True) as scratch:
                    for script in scripts:
                        scratch.execute(script.read_text())
                    found.append(scratch.execute(catalog_sql).fetchall())
            finally:
                admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    return found[0], found[1]


def test_the_migrations_build_the_same_schema_as_001_schema(settings):
    direct, migrated = catalogs(settings)
    assert len(direct) > 80
    assert migrated == direct
