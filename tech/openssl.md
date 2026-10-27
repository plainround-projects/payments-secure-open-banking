# OpenSSL (the command-line tool)

## What it is

OpenSSL is a widely installed cryptography toolkit. Besides the library that many programs use, it ships a command-line program, `openssl`, with many sub-commands. This repository uses exactly one: `openssl rand`, which prints random bytes from the operating system's secure generator.

## Why this repository uses it

The bank needs a secret value, the **pepper**, to key its digests of one-time codes and access tokens (see `hashing-hmac-and-scrypt.md`). It must be random, different on every machine and never committed to git. `make up` creates it the first time with one `openssl` call, so nobody ever has to invent or paste a secret.

## Where it appears

| Path | What it does with OpenSSL |
| --- | --- |
| `Makefile`, target `env` | `pepper=$$(openssl rand -hex 24)` generates the pepper and writes it into `.env`. |
| `.env.example` | Holds a placeholder line for `BANK_PEPPER`; the real value is generated. |
| `.github/workflows/ci.yml` | CI does not use it; it sets a throwaway pepper instead. |

(Elsewhere in the repository, keys are made with Python's `cryptography` package, not the `openssl` program; see `cryptography.md`.)

## How it works underneath

`openssl rand -hex 24` asks the operating system for 24 random bytes (from its cryptographically secure source) and prints them as hexadecimal text: each byte becomes two characters from `0-9a-f`, so the output is 48 characters plus a newline.

## Walkthrough of this repository's use

```make
env:
	@[ -f .env ] || { cp .env.example .env; \
	  pepper=$$(openssl rand -hex 24); sed -i.bak "s/^BANK_PEPPER=.*/BANK_PEPPER=$$pepper/" .env && rm -f .env.bak; \
	  echo "created .env from .env.example with a generated BANK_PEPPER"; }
```

- `openssl rand -hex 24`: 24 random bytes as hex. 24 bytes is 192 bits, far more than the 16-character minimum that the bank's configuration check requires (`BANK_PEPPER must be at least 16 characters`).
- `$$( ... )` is command substitution (the doubled dollar is for `make`): the printed value is captured into the shell variable `pepper`.
- `sed` then writes it into the copied `.env` file in place of the placeholder (see `shell.md` for the flags).
- Because the block only runs when `.env` does not exist, an existing pepper is never replaced; replacing it would invalidate every stored token digest and code digest.

## Try it yourself

1. See what it prints:

   ```bash
   openssl rand -hex 24
   ```

   Expected: a different 48-character hex string each time, for example `3f9a...` (48 characters).

2. Count the characters (the output includes the newline):

   ```bash
   openssl rand -hex 24 | wc -c
   ```

   Expected: `49`.

3. Check the version on your machine: `openssl version`. Any recent OpenSSL or LibreSSL supports `rand -hex`.

4. Find where the generated pepper landed (after `make up`): `grep -c "^BANK_PEPPER=" .env` prints `1`, and `grep "^BANK_PEPPER=" .env | wc -c` is 61 (the name, the equals sign, 48 characters and the newline).

## Mistakes people make

- **Generating a "random" secret with `date` or `$RANDOM`.** They are predictable. `openssl rand` and Python's `secrets` are not.
- **Printing the secret in logs.** The Makefile line is prefixed with `@` so the command, which contains the value after expansion, is not echoed.
- **Committing `.env`.** It is in `.gitignore`.
- **Regenerating the pepper by accident.** The `[ -f .env ] ||` guard prevents it.

## Check yourself

1. What does `openssl rand -hex 24` print? *48 hexadecimal characters from 24 random bytes.*
2. Why is the pepper generated instead of committed? *A committed secret is not a secret, and every checkout should have its own.*
3. What would happen to stored digests if the pepper changed? *They could no longer be recomputed or matched, so existing codes and tokens would stop working.*
4. Does this repository use the `openssl` command to make signing keys? *No; Python's `cryptography` does.*
5. Why does the Makefile line start with `@`? *To stop make from echoing the command, which would show the generated secret.*

## Further reading

- OpenSSL command-line tools: https://docs.openssl.org/master/man1/
- `openssl rand`: https://docs.openssl.org/master/man1/openssl-rand/
