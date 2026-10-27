# psql

## What it is

`psql` is PostgreSQL's interactive **command-line client**. You give it a connection (host, user, database); it then reads SQL, sends it to the server and prints the result as a table. Besides SQL it understands its own **meta-commands** that start with a backslash, such as `\dt` (list tables) and `\echo` (print text).

## Why this repository uses it

It is the simplest way to load the SQL files into the database and to look at the result, and it is already inside the PostgreSQL container, so nothing needs installing on the computer. The Makefile feeds every SQL file to it; the README and the notes use it for exploration.

## Where it appears

| Path | Role |
| --- | --- |
| `Makefile` | `PSQL := docker compose exec -T postgres psql ... -v ON_ERROR_STOP=1 -q`, used by `reset-db`, `db`, `migrate`, `queries`. |
| `sql/003_queries.sql` | Uses `\echo` headings, which only psql understands. |
| `docker-compose.yml` | The health check uses `pg_isready`, a sibling tool from the same package. |

## How it works underneath

`psql` is an ordinary program. It opens a TCP or socket connection to the server (here, from inside the container, through the socket), authenticates as the role given by `-U`, and reads statements from standard input or an interactive prompt. It splits input at `;`, sends each statement and prints the answer. Lines starting with `\` are handled by psql itself and never reach the server.

## Walkthrough of this repository's use

```make
PSQL := docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -q
```

| Piece | Meaning |
| --- | --- |
| `docker compose exec -T postgres` | Run the following command inside the running `postgres` service; `-T` disables the terminal so file redirection works. |
| `-U $(POSTGRES_USER)` | Connect as this role (`openbanking`). |
| `-d $(POSTGRES_DB)` | Connect to this database (`openbanking`). |
| `-v ON_ERROR_STOP=1` | Set the psql variable `ON_ERROR_STOP` so the first error ends the run with a non-zero exit status. By default psql continues after an error and exits 0, which would hide a broken script. |
| `-q` | Quiet: do not print `CREATE TABLE`, `INSERT 0 1` and similar tags. |

Typical use:

```make
db: reset-db
	$(PSQL) < sql/001_schema.sql
	$(PSQL) < sql/002_seed.sql
```

The shell sends each file into psql's standard input. `reset-db` first runs `sql/reset.sql`, which drops and recreates the `public` schema.

```make
queries:
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 < sql/003_queries.sql
```

Without `-q` here, so the results print. The file's `\echo '== 1. Trial balance ...'` lines print headings between queries.

`migrate` connects to the scratch database `openbanking_migrated` instead, by `-d openbanking_migrated`, after creating it with `-c "CREATE DATABASE openbanking_migrated"`. `-c "command"` runs one command and exits.

Meta-commands worth knowing: `\dt` lists tables, `\d table` describes one, `\x` toggles expanded output, `\q` quits.

## Try it yourself

1. Open an interactive session:

   ```bash
   docker compose exec postgres psql -U openbanking -d openbanking
   ```

   Then `\dt` (twelve tables, from `access_tokens` to `seen_proofs`) and `select account_id, balance_minor from bank_accounts order by 1;` Expected after `make db`: five accounts, `acct-ada` 50000, `acct-bob` 20000, `acct-other-shop` 0, `acct-shop` 0 and `acct-vault` -70000. Leave with `\q`.

2. Describe a table: inside psql, `\d postings`. Expected: the columns, indexes, check constraints, foreign keys and the triggers (`postings_append_only`, `postings_entry_balanced`).

3. See `ON_ERROR_STOP` in action:

   ```bash
   printf 'select 1;\nselect * from nope;\nselect 2;\n' | docker compose exec -T postgres psql -U openbanking -d openbanking -v ON_ERROR_STOP=1
   echo "exit=$?"
   ```

   Expected: `1`, then `ERROR:  relation "nope" does not exist`, no `2`, and `exit=3`. Without `-v ON_ERROR_STOP=1` the `2` prints too and the exit status is 0.

## Mistakes people make

- **Forgetting `ON_ERROR_STOP`.** A broken schema script "succeeds".
- **Leaving out `-T` when feeding a file or a pipe to `docker compose exec`.** Compose then tries to attach a terminal and complains that the input device is not a TTY.
- **Typing SQL without the final `;`** in the interactive prompt and wondering why nothing runs.
- **Running a reset against the wrong database.** `reset.sql` drops the `public` schema; this repository only points it at its own disposable database.

## Check yourself

1. What does `-v ON_ERROR_STOP=1` do? *Stops at the first error and exits non-zero.*
2. What is `\echo`? *A psql meta-command that prints text; the server never sees it.*
3. Why `-T` with Compose `exec`? *No terminal is allocated, which input redirection needs.*
4. How does `make db` give psql a file? *With shell redirection `<`, so the file becomes standard input.*
5. What does `-c` do? *Runs one command and exits.*

## Further reading

- psql (PostgreSQL 18): https://www.postgresql.org/docs/18/app-psql.html
- `pg_isready`: https://www.postgresql.org/docs/18/app-pg-isready.html
