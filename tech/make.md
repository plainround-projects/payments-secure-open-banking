# Make

## What it is

`make` is a command-line tool that runs named recipes from a file called `Makefile`. Each recipe is a **target** (a name), its **prerequisites** (things that must exist or be up to date first) and a list of shell commands. Make was designed to rebuild only what is out of date, by comparing file times. Here it is mostly used as a tidy menu of project commands: `make up`, `make run`, `make test`.

## Why this repository uses it

The lesson needs the same few steps every time: start the database, load the schema, run the scenario, test, lint, stop. One short name per step means a reader never has to remember long commands, and CI runs exactly the same commands (`make check`, `make audit`, `make test-integration`).

## Where it appears

| Path | Role |
| --- | --- |
| `Makefile` | All targets (114 lines). |
| `.github/workflows/ci.yml` | Calls `make check`, `make audit` and `make test-integration test-e2e`. |
| `README.md` | Documents the targets as the way to use the project. |

## How it works underneath

1. `make up` reads `Makefile` from the current folder and looks for the target `up`.
2. It first brings each prerequisite up to date (recursively). For a **file** target it compares timestamps: if a prerequisite is newer than the target, the recipe runs. For a **phony** target (not a file) it always runs.
3. Each recipe line is run by `/bin/sh -c` in its **own** shell, so a `cd` or a variable set on one line is gone on the next line.
4. If a command exits with a non-zero status, make stops with an error, unless the line starts with `-`.
5. Recipe lines must start with a **tab** character, not spaces (`.editorconfig` has `[Makefile] indent_style = tab` for this reason).

## Walkthrough of this repository's use

### Variables

```make
PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3)
VENV := .venv
PY := $(VENV)/bin/python
STAMP := $(VENV)/.installed
POSTGRES_USER ?= openbanking
PSQL := docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -q
```

- `?=` sets the variable only if it has no value yet, so you can override it: `make PYTHON=python3.12 install`.
- `:=` evaluates the right side once, immediately.
- `$(shell ...)` runs a command and uses its output. `command -v NAME` prints the path of `NAME` if it exists; `||` tries the next one if it does not.
- `$(VAR)` inserts a variable.

### Reading `.env`

```make
-include .env
export
```

`include` pastes another file into the Makefile (here `.env` holds lines like `NAME=value`). The leading `-` means "do not fail if the file is missing". A bare `export` makes every make variable an **environment variable** of the commands it runs, so Docker Compose and the Python programs see `POSTGRES_PASSWORD` and the rest.

### Phony targets

```make
.PHONY: help install lock env up db reset-db migrate queries run serve health lint format test ...
```

Declares that these names are commands, not files. Without it, a file named `test` in the folder would make `make test` say "nothing to do".

### The self-documenting `help`

```make
help: ## list the targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort
```

- A `## text` comment after a target is its description.
- `@` in front of a recipe line hides the command itself.
- `$(MAKEFILE_LIST)` is the list of makefiles read (`Makefile` and `.env`). `grep -h` prints matching lines without the file name; without `-h`, two files would make grep prefix each line with `Makefile:`.
- `sed` replaces `: ... ## ` with a tab, leaving `target<TAB>description`; `sort` orders them.

### A file target with prerequisites: the stamp

```make
$(STAMP): requirements-dev.lock pyproject.toml
	$(PYTHON) -m venv $(VENV)
	...
	touch $@

install: $(STAMP) ## create .venv from the lock file
```

`.venv/.installed` is an empty file. It is rebuilt only when a lock file or `pyproject.toml` is newer. `$@` is the name of the target. Other targets list `$(STAMP)` as a prerequisite so the environment exists first.

### Calling targets and handling errors

```make
up: env $(STAMP) ## start PostgreSQL and load sql/001_schema.sql and sql/002_seed.sql
	docker compose up -d --wait postgres
	$(MAKE) --no-print-directory db
```

- `$(MAKE)` re-runs make itself (always use it for this, never the word `make`). `--no-print-directory` stops the "Entering directory" noise.
- `run: $(STAMP) db` lists `db` as a prerequisite, so the database is rebuilt every time.
- A recipe line starting with `-` ignores its error: `-docker compose exec ... "DROP DATABASE IF EXISTS openbanking_migrated"`.

### Shell loops inside a recipe

```make
	@for file in sql/migrations/*.sql; do echo "applying $$file"; \
	  docker compose exec -T postgres psql ... < $$file; done
```

`$$` is how you write a single `$` for the shell (make would otherwise treat `$file` as its own variable). The backslash at the end of a line continues the same shell command on the next line.

### Targets that start programs

```make
run: $(STAMP) db ## the lesson's scenario end to end, including the failures (resets the database)
	rm -rf var
	$(VENV)/bin/open-banking-demo

register: $(STAMP) ## register merchant-1 and enroll ada and bob (writes secrets under var/, git-ignored)
	$(VENV)/bin/open-banking-admin register-merchant --client-id merchant-1 --name "Shop"
	$(VENV)/bin/open-banking-admin enroll-customer --username ada --account acct-ada
	$(VENV)/bin/open-banking-admin enroll-customer --username bob --account acct-bob

serve-bank: $(STAMP)
	$(VENV)/bin/open-banking-bank
```

- `run` rebuilds the database (`db` is a prerequisite), deletes the generated folder `var`, and runs the demo, which starts a bank and a merchant server inside its own process.
- `register`, `serve-bank` and `serve-merchant` are the same system started by hand as separate processes: register the credentials first, then run the bank in one terminal and the merchant in another. Each program is the command created from `[project.scripts]`.

### Test targets

```make
test: $(STAMP)
	$(PY) -m pytest tests/unit --cov=open_banking --cov-report=term
	$(PY) -m coverage report --include='src/open_banking/domain/*,src/open_banking/application/*' --fail-under=95
```

The second line makes `make test` fail if domain and application coverage drops below 95 percent.

### `check`

`check: lint test` has no recipe; it only lists prerequisites, so it runs both. CI calls it.

<!-- listing:Makefile -->
### `Makefile` (114 lines)

```make
# make up | db | run | serve-bank | serve-merchant | queries | test | test-integration | test-e2e | lint | audit | down   (make help lists all)
PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3)
VENV := .venv
PY := $(VENV)/bin/python
STAMP := $(VENV)/.installed
POSTGRES_USER ?= openbanking
POSTGRES_DB ?= openbanking
PSQL := docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -q
```

The first line is a comment listing the targets (`make up`, `make db`, ...). `PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3)`: `?=` assigns only if the variable has no value yet, so you can override it on the command line; `$(shell ...)` runs a shell command and uses its output; `command -v name` prints where a program is; `||` tries the next alternative when the previous one printed nothing. So the newest of the three Pythons found is used. `VENV := .venv` names the virtual-environment folder (`:=` evaluates immediately); `PY` is the interpreter inside it; `STAMP` is a marker file `.venv/.installed`; `POSTGRES_USER` and `POSTGRES_DB` default to `openbanking`. `PSQL` is the command that runs `psql` inside the database container (`-T` means no terminal, `-v ON_ERROR_STOP=1` stops at the first SQL error, `-q` is quiet).

```make
-include .env
export
```

`-include .env` reads the file `.env` (variables of the form `NAME=value`) into make, and the leading dash means "no error if it does not exist". A bare `export` then passes every make variable to the programs make starts, so the Python programs and Docker Compose see the password and ports.

```make
.PHONY: help install lock env up db reset-db migrate queries run register serve-bank serve-merchant health lint format test \
        test-integration test-e2e audit check docker-build down clean
```

`.PHONY:` lists names that are commands and not files, so a file called `test` can never stop `make test` from running. The backslash continues the line.

```make
help: ## list the targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort
```

`help` prints the targets. Every target with a `## description` comment is found by `grep -hE '^[a-z-]+:.*## '` over the makefiles (`-h` hides file names, `-E` enables extended patterns), `sed` rewrites `target: ... ## text` into `target<TAB>text`, and `sort` orders them. The leading `@` stops make from printing the command itself.

```make
$(STAMP): requirements-dev.lock pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q --require-hashes -r requirements-dev.lock
	$(VENV)/bin/pip install -q --no-deps -e .
	touch $@

install: $(STAMP) ## create .venv from the lock file
```

The environment target. `$(STAMP): requirements-dev.lock pyproject.toml` says the file `.venv/.installed` depends on those two files; make runs the recipe again only when either is newer. The recipe lines (each starts with a tab): create the virtual environment (`-m venv`), install exactly the locked packages (`pip install -q --require-hashes -r requirements-dev.lock`: `-q` quiet, `--require-hashes` accepts only files whose hash is in the lock), install this project itself in editable mode without resolving dependencies (`--no-deps -e .`), and `touch $@` creates the stamp (`$@` means the target name). `install` just depends on the stamp.

```make
lock: ## regenerate the hash-pinned lock files from pyproject.toml (needs uv)
	uv pip compile pyproject.toml --generate-hashes --python-version 3.12 -o requirements.lock
	uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 -o requirements-dev.lock

# .env is created from .env.example; the pepper that keys the bank's digests is generated here, never committed.
```

`lock` regenerates the two lock files from `pyproject.toml` with `uv pip compile ... --generate-hashes --python-version 3.12 -o ...`. The second line adds `--extra dev` for the development tools. It needs the tool `uv`.

```make
env:
	@[ -f .env ] || { cp .env.example .env; \
	  pepper=$$(openssl rand -hex 24); sed -i.bak "s/^BANK_PEPPER=.*/BANK_PEPPER=$$pepper/" .env && rm -f .env.bak; \
	  echo "created .env from .env.example with a generated BANK_PEPPER"; }
```

`env` creates `.env` from `.env.example` only if it does not exist, and **generates the pepper** in the process. The comment above says why: the pepper that keys the bank's digests is generated here and never committed. `[ -f .env ] || { ...; }` runs the braces only when the file is missing (`[ -f file ]` tests that the file exists; `||` runs the next part only when the test failed). Inside: `cp .env.example .env`; `pepper=$$(openssl rand -hex 24)` stores 24 random bytes written as 48 hexadecimal characters in a shell variable (`$$` is how a dollar sign is written inside a makefile, and `$( ... )` runs a command and uses its output); `sed -i.bak "s/^BANK_PEPPER=.*/BANK_PEPPER=$$pepper/" .env` replaces the line that starts with `BANK_PEPPER=` by one with the generated value (`-i.bak` edits in place and keeps a backup with that ending, which works on both macOS and Linux sed) and `rm -f .env.bak` removes the backup; finally an `echo` says what happened. The backslashes continue the command over several lines.

```make
up: env $(STAMP) ## start PostgreSQL and load sql/001_schema.sql and sql/002_seed.sql
	docker compose up -d --wait postgres
	$(MAKE) --no-print-directory db
```

`up` starts the database: it needs `.env` and the environment, then `docker compose up -d --wait postgres` starts the `postgres` service in the background (`-d`) and waits until its health check passes (`--wait`), then `$(MAKE) --no-print-directory db` runs the `db` target.

```make
reset-db:
	$(PSQL) < sql/reset.sql

db: reset-db ## recreate the schema from sql/001_schema.sql and load sql/002_seed.sql (drops existing data)
	$(PSQL) < sql/001_schema.sql
	$(PSQL) < sql/002_seed.sql

# The lesson's stepwise path: apply sql/migrations/*.sql, in order, to a scratch database (openbanking_migrated).
```

`reset-db` runs `sql/reset.sql` through `psql` (the `< file` feeds the file to its standard input). `db` depends on it, then loads the schema and the seed the same way.

```make
migrate: ## apply sql/migrations/*.sql to a scratch database and list its tables
	-docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -q -c "DROP DATABASE IF EXISTS openbanking_migrated"
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -q -c "CREATE DATABASE openbanking_migrated"
	@for file in sql/migrations/*.sql; do echo "applying $$file"; \
	  docker compose exec -T postgres psql -U $(POSTGRES_USER) -d openbanking_migrated -v ON_ERROR_STOP=1 -q < $$file; done
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d openbanking_migrated -c "\dt"
```

`migrate` applies the stepwise migrations to a scratch database: the first line drops `openbanking_migrated` if it exists (the leading `-` ignores a failure), the second creates it, the loop `for file in sql/migrations/*.sql; do ...; done` applies each file in sorted order (`$$file` is how a shell variable is written inside a makefile), and the last line lists the tables.

```make
queries: ## run sql/003_queries.sql
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 < sql/003_queries.sql

# The lesson's whole scenario with a bank and a merchant server in this process: consent, refusals, replay, tampering,
# abandonment, revocation, a lost response, and the stretch goals. Resets the database first.
```

`queries` runs `sql/003_queries.sql`.

```make
run: $(STAMP) db ## the lesson's scenario end to end, including the failures (resets the database)
	rm -rf var
	$(VENV)/bin/open-banking-demo
	$(MAKE) --no-print-directory queries

# Register the demo merchant and customers by hand, to run the bank and merchant as separate processes.
```

`run` is the lesson's scenario with a bank and a merchant in one process. The comment above lists what it shows: consent, refusals, replay, tampering, abandonment, revocation, a lost response and the stretch goals. It depends on the stamp and on `db` (so the database is rebuilt first). Its recipe: `rm -rf var` removes the folder of keys and secrets from earlier runs, `open-banking-demo` runs the scenario, and `$(MAKE) --no-print-directory queries` runs the SQL queries afterwards.

```make
register: $(STAMP) ## register merchant-1 and enroll ada and bob (writes secrets under var/, git-ignored)
	$(VENV)/bin/open-banking-admin register-merchant --client-id merchant-1 --name "Shop"
	$(VENV)/bin/open-banking-admin enroll-customer --username ada --account acct-ada
	$(VENV)/bin/open-banking-admin enroll-customer --username bob --account acct-bob
```

`register` prepares the demo by hand so the bank and the merchant can run as separate processes. The comment above says so. It calls the admin program three times: register the merchant `merchant-1` named "Shop" (the quotes keep the name as one argument), and enroll the customers `ada` and `bob` with their accounts. The secrets (the merchant's key and client secret, each customer's PIN) are written under `var/`, which git ignores; they are not printed.

```make
serve-bank: $(STAMP) ## run the bank API in the foreground (Ctrl-C or SIGTERM stops it gracefully)
	$(VENV)/bin/open-banking-bank
```

`serve` runs the HTTP API in the foreground, using the command installed into the virtual environment. `health` runs the health command.

```make
serve-merchant: $(STAMP) ## run the merchant API in the foreground (needs make register first)
	set -a; . var/merchant-1.env; set +a; $(VENV)/bin/open-banking-merchant
```

`serve-merchant` runs the merchant API in the foreground and needs `make register` first. `set -a; . var/merchant-1.env; set +a;` reads the merchant's environment file written by `register` (`set -a` marks every variable defined from now on for export to programs; `.` reads a file as shell commands; `set +a` turns the marking off), and then starts the merchant program with those variables.

```make
health: $(STAMP) ## exit 0 when the running bank reports healthy
	$(VENV)/bin/open-banking-health
```

`health` asks the running bank whether it is healthy (exit 0 if it is).

```make
lint: $(STAMP) ## ruff check, ruff format check, mypy, import-linter
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy
	$(VENV)/bin/lint-imports
```

`lint` runs, in order: `ruff check .` (style and likely bugs), `ruff format --check .` (is the layout canonical), `mypy` (types) and `lint-imports` (the layering rules). The first failure stops make.

```make
format: $(STAMP) ## apply ruff's formatting and safe fixes
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .

# Unit tests need no services. The domain and application must stay at 95% coverage or more.
```

`format` applies Ruff's safe fixes and formatting.

```make
test: $(STAMP) ## unit tests with coverage (no database needed)
	$(PY) -m pytest tests/unit --cov=open_banking --cov-report=term
	$(PY) -m coverage report --include='src/open_banking/domain/*,src/open_banking/application/*' --fail-under=95
```

`test` runs the unit tests with coverage (`--cov=open_banking --cov-report=term`) and then fails if the coverage of the domain and application packages is under 95 percent (`coverage report ... --fail-under=95`).

```make
test-integration: $(STAMP) ## adapters and the lesson's rules against the running database (make up first)
	$(PY) -m pytest tests/integration -m integration
```

`test-integration` runs the tests marked `integration` against the running database; `test-e2e` runs the tests marked `e2e`, which start a real server process.

```make
test-e2e: $(STAMP) ## the scenario against real bank and merchant server processes (make up first)
	$(PY) -m pytest tests/e2e -m e2e
```

The end-to-end tests.

```make
audit: $(STAMP) ## check the locked dependencies for known vulnerabilities
	$(VENV)/bin/pip-audit --no-deps --disable-pip -r requirements-dev.lock
```

`audit` checks the locked packages for known vulnerabilities (`pip-audit --no-deps --disable-pip -r requirements-dev.lock`).

```make
check: lint test ## everything that needs no services
```

`check` has no recipe of its own: it depends on `lint` and `test`, so it runs both. CI calls it.

```make
docker-build: ## build the container image of the bank and the merchant
	docker build -t payments-secure-open-banking:dev .
```

`docker-build` builds the API image with `docker build -t payments-secure-open-banking:dev .`.

```make
down: ## stop PostgreSQL and delete its data
	docker compose --profile app down -v
```

`down` stops the containers and deletes their data (`docker compose --profile app down -v`).

```make
clean: down
	rm -rf $(VENV) logs var .pytest_cache .mypy_cache .ruff_cache .hypothesis .coverage
```

`clean` depends on `down` and then deletes the virtual environment, logs, the `var` folder of secrets, and the caches and coverage data with `rm -rf`.

<!-- /listing:Makefile -->

## Try it yourself

1. List the commands:

   ```bash
   make help
   ```

   Expected: lines such as `audit<TAB>check the locked dependencies for known vulnerabilities`, sorted by name.

2. See what a target would do without doing it:

   ```bash
   make -n test
   ```

   Expected: the two `python -m ...` commands printed, not run.

3. Watch the stamp logic: `make install` (quick if up to date), then `touch pyproject.toml && make install -n` shows the rebuild commands.

4. Override a variable: `make -n up POSTGRES_USER=other`.

## Mistakes people make

- **Spaces instead of a tab** at the start of a recipe line: `missing separator`.
- **Expecting `cd` to carry over** between recipe lines. Each line is its own shell.
- **Writing `$var` instead of `$$var`** for a shell variable.
- **Forgetting `.PHONY`**, so a same-named file blocks the target.
- **Typing `make` where `$(MAKE)` is needed** inside a recipe.

## Check yourself

1. Why does each target need a tab-indented recipe? *It is the Makefile syntax; spaces cause `missing separator`.*
2. What does `$@` mean? *The name of the target being built.*
3. What does `-include .env` do if `.env` does not exist? *Nothing; the `-` ignores the missing file.*
4. Why `$$file` in the migrate loop? *`$$` passes a literal `$` to the shell so the shell variable is used.*
5. Why does `make help` use `grep -h`? *Without it, with two files in `MAKEFILE_LIST`, each line would carry a filename prefix and the target name would be lost.*

## Further reading

- GNU Make manual (the Makefile on macOS and Linux is read by GNU Make 3.81 or newer): https://www.gnu.org/software/make/manual/
- Phony targets: https://www.gnu.org/software/make/manual/html_node/Phony-Targets.html
- Automatic variables: https://www.gnu.org/software/make/manual/html_node/Automatic-Variables.html
