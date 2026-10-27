-- reset.sql: drop everything so `make db` starts from an empty database. The ledger is append-only, so a reset is
-- a schema drop, not a DELETE. Local development only.
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
