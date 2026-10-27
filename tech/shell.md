# The shell (sh and Bash)

## What it is

A **shell** is a program that reads a line of text, treats the first word as a program to run and the other words as its arguments, and starts it. It also has a small language of its own: pipes, redirection, variables, loops and tests. Bash is the common shell on Linux; `sh` is the simpler standard one that `make` uses to run recipes. This repository has no stand-alone `.sh` file, but shell syntax appears in the Makefile, the Compose health check and the CI workflow, so a reader needs to follow it.

## Why this repository uses it

Starting a database, loading SQL files into it and checking it is healthy are all "run a program and look at its exit status" jobs. The shell is the glue that does this in a few words.

## Where it appears

| Path | Shell used |
| --- | --- |
| `Makefile` | Every recipe line is run by `/bin/sh -c`: the `[ -f .env ] || { ... }` test with `openssl` and `sed` in `env`, the `set -a; . var/merchant-1.env; set +a` line in `serve-merchant`, redirection `< sql/001_schema.sql`, the `for` loop in `migrate`, the pipe in `help`. |
| `docker-compose.yml` | The health check `["CMD-SHELL", "pg_isready ... $${POSTGRES_USER}"]` is run through a shell inside the container. |
| `.github/workflows/ci.yml` | Each `run:` line is a shell command on the CI machine. |

## How it works underneath

- Every program ends with an **exit status**, a number. `0` means success, anything else means failure. The shell stores the last status in `$?`.
- `A && B` runs B only if A succeeded. `A || B` runs B only if A failed. `A; B` runs both.
- **Standard input** (stdin) is where a program reads, **standard output** (stdout) is where it writes, and **standard error** (stderr) is for messages about problems. `<` feeds a file into stdin. `>` sends stdout to a file. `|` connects one program's stdout to the next one's stdin.
- Before running a command, the shell expands variables (`$NAME`), patterns (`*.sql`) and quotes.

## Walkthrough of this repository's use

### Creating `.env` once, with a generated secret

```make
env:
	@[ -f .env ] || { cp .env.example .env; \
	  pepper=$$(openssl rand -hex 24); sed -i.bak "s/^BANK_PEPPER=.*/BANK_PEPPER=$$pepper/" .env && rm -f .env.bak; \
	  echo "created .env from .env.example with a generated BANK_PEPPER"; }
```

- `[ -f .env ]` is the `test` command: true when `.env` is a regular file. The spaces inside the brackets are required.
- `||` means: if the file does **not** exist (the test failed), run what follows. So an existing `.env` is never touched.
- `{ ...; ...; }` groups the commands. The trailing `\` joins the lines, so make hands the shell one long command.
- `cp .env.example .env` copies the template.
- `pepper=$$(openssl rand -hex 24)` runs `openssl rand -hex 24` (see `openssl.md`), which prints 24 random bytes as 48 hexadecimal characters, and stores the output in the shell variable `pepper`. `$(...)` is **command substitution**: the output of the command replaces the expression. The doubled `$$` is make's way to write a single `$` for the shell.
- `sed -i.bak "s/^BANK_PEPPER=.*/BANK_PEPPER=$$pepper/" .env` edits `.env` in place: the `s/old/new/` command replaces any line that starts (`^`) with `BANK_PEPPER=` by the same name and the new value. `-i.bak` means "edit in place and keep a backup named `.env.bak`"; writing the suffix directly after `-i` works on both macOS (BSD) `sed` and GNU `sed`, whereas a bare `-i` does not.
- `&& rm -f .env.bak` removes the backup if `sed` succeeded (`-f` means no error if it is missing).
- `echo` reports what happened. The leading `@` is make's: it hides the command line itself, which would otherwise print the secret.

The secret is therefore random per checkout, never committed, and the committed `.env.example` holds only a placeholder.

### Feeding SQL files to PostgreSQL

```make
PSQL := docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -q
...
	$(PSQL) < sql/001_schema.sql
```

- `docker compose exec -T postgres psql ...` runs the `psql` client inside the running container. `-T` turns off the pseudo-terminal, which is needed when stdin is a file.
- `< sql/001_schema.sql` opens the file on the host and connects it to the program's stdin, so `psql` reads the SQL from it.
- `-v ON_ERROR_STOP=1` is a psql variable: stop at the first error and exit non-zero, so `make` notices a broken script.
- `-q` is quiet.

### A loop over files

```make
	@for file in sql/migrations/*.sql; do echo "applying $$file"; \
	  docker compose exec -T postgres psql -U $(POSTGRES_USER) -d openbanking_migrated -v ON_ERROR_STOP=1 -q < $$file; done
```

- `sql/migrations/*.sql` is a **glob**; the shell replaces it with the sorted list of matching names (`001_accounts_and_ledger.sql`, `002_customers_and_consent.sql`, ...). The numeric prefixes make sorted order equal to the order to apply.
- `for file in LIST; do ...; done` repeats the body for each name.
- `$$file` is make's way to write `$file`. The trailing `\` continues the line.

### Loading a secrets file into one command

```make
register: $(STAMP)
	$(VENV)/bin/open-banking-admin register-merchant --client-id merchant-1 --name "Shop"
	...
serve-merchant: $(STAMP)
	set -a; . var/merchant-1.env; set +a; $(VENV)/bin/open-banking-merchant
```

`make register` runs the admin program, which creates the merchant's private key and writes `var/merchant-1.env` containing three lines of the form `NAME=value` (the client id, the client secret, the key file path). The file is created readable by its owner only (mode `0600`) and `var/` is git-ignored. `serve-merchant` then:

- `set -a` turns on **auto-export**: every variable assigned from now on is passed on to the programs the shell starts.
- `. var/merchant-1.env` (a dot, a space, a file name) reads the file **in the current shell**, executing each `NAME=value` line. It is the same as the word `source` in Bash.
- `set +a` turns auto-export off again.
- `$(VENV)/bin/open-banking-merchant` starts the merchant with those variables in its environment, so the secret travels in memory and never appears on the command line (where `ps` would show it).

`run` also begins with `rm -rf var`, which deletes the folder of generated keys, PIN files and one-time-code outbox so that every demo starts from nothing.

### A health check through a shell

```yaml
test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
```

`CMD-SHELL` hands the string to `/bin/sh -c` inside the container, so `${POSTGRES_USER}` is read from that container's environment. The doubled `$$` stops Docker Compose from filling the variable in itself while reading the YAML. `pg_isready` exits `0` when the server accepts connections. `-h 127.0.0.1` forces a network connection, because during first start the database briefly runs a temporary server that only listens on a socket; without it the check can report healthy too early.

### A pipe in `help`

```make
@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort
```

Three programs in a row: `grep` selects lines, `sed` rewrites them, `sort` orders them. `-E` means extended regular expressions; `^` is "start of line"; `[a-z-]+` is one or more lowercase letters or hyphens.

### Quoting

Single quotes (`'...'`) keep every character as it is. Double quotes (`"..."`) still expand `$VAR`. The repository uses single quotes around regular expressions so the shell does not touch `$`, `*` and `^`.

## Try it yourself

1. Exit statuses:

   ```bash
   true; echo $?; false; echo $?
   ```

   Expected: `0` then `1`.

2. The `||` idiom:

   ```bash
   [ -f /nope ] || echo "missing"
   ```

   Expected: `missing`.

3. A glob in the order the migrations run:

   ```bash
   echo sql/migrations/*.sql
   ```

   Expected: `sql/migrations/001_accounts_and_ledger.sql sql/migrations/002_customers_and_consent.sql sql/migrations/003_merchant_access.sql sql/migrations/004_payments.sql`.

4. A pipe, the same shape as `help`:

   ```bash
   make help | sort | head -3
   ```

   Expected: the first three target names with descriptions.

## Mistakes people make

- **Unquoted variables with spaces.** `rm $file` splits on spaces; write `rm "$file"`.
- **Forgetting that `||` runs on failure.** `[ -f .env ] || cp ...` copies when the file is **missing**.
- **Using `$name` in a Makefile for a shell variable.** Write `$$name`.
- **Ignoring failures in a loop.** In `migrate`, `-v ON_ERROR_STOP=1` makes psql exit non-zero so the loop's failing command stops make.
- **Assuming Bash features inside `sh`.** Make uses `/bin/sh`; this repository sticks to plain POSIX syntax (no arrays, no `[[ ]]`).

## Check yourself

1. What does an exit status of `0` mean? *Success.*
2. What does `< file` do? *It makes the program read the file as its standard input.*
3. Why `$${POSTGRES_USER}` in the Compose file? *`$$` escapes Compose's own substitution so the container's shell sees `${POSTGRES_USER}`.*
4. What does `A || B` do? *Runs B only if A failed.*
5. Why does the migration loop apply files in the right order? *The shell expands the glob in sorted order and the filenames start with 001, 002, ...*

## Further reading

- POSIX shell command language (what `sh` implements): https://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap02.html
- GNU Bash manual (macOS and Linux Bash 3.2 or newer): https://www.gnu.org/software/bash/manual/
- `psql` options (PostgreSQL 18): https://www.postgresql.org/docs/18/app-psql.html
- `pg_isready`: https://www.postgresql.org/docs/18/app-pg-isready.html
