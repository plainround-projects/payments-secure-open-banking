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
