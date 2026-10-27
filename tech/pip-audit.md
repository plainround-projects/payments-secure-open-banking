# pip-audit

## What it is

pip-audit is a command-line tool that checks the Python packages a project uses against a public database of **known vulnerabilities** (security holes that someone has reported, each with an identifier such as a CVE number). It reads a list of package names and versions, looks each up, and prints any that have a known problem and which version fixes it.

## Why this repository uses it

A payments service handles money; a vulnerable library underneath it is a way in. Because the lock files pin exact versions, an audit of those exact versions is meaningful: it says whether the code you actually ship contains a known-bad package. `make audit` runs in CI on every push.

## Where it appears

| Path | Role |
| --- | --- |
| `Makefile` target `audit` | Runs `pip-audit` against `requirements-dev.lock`. |
| `.github/workflows/ci.yml` | The `check` job runs `make audit` after `make check`. |
| `requirements-dev.lock` | The input: every package with an exact version and hashes. |
| `pyproject.toml` | `pip-audit>=2.7` in the `dev` extra. |

## How it works underneath

pip-audit takes each `name==version` and asks a vulnerability service (by default the Python Packaging Advisory Database, through PyPI) whether that version is affected by any advisory. This needs network access. It prints a table when it finds something, and exits non-zero so a build fails.

## Walkthrough of this repository's use

```make
audit: $(STAMP) ## check the locked dependencies for known vulnerabilities
	$(VENV)/bin/pip-audit --no-deps --disable-pip -r requirements-dev.lock
```

- `-r requirements-dev.lock`: audit the packages listed in that file (the whole dependency tree, including development tools).
- `--no-deps`: do not look for further dependencies. The lock already lists all of them; resolving again could disagree with it.
- `--disable-pip`: do not call pip to install anything into a temporary environment. With a fully pinned input, pip-audit only needs to read the file.

With `--no-deps`, pip-audit prints two warnings (`--no-deps is supported, but users are encouraged to fully hash their pinned dependencies` and a hint to use `pip-compile`). They are advice about the input file, not findings. The line that matters is the last one:

```
No known vulnerabilities found
```

If a package were affected, the output would be a table with the package, the installed version, the advisory id and the version that fixes it. The fix is to update the lock (`make lock`) to a patched version, run the tests and commit.

## Try it yourself

1. Run it (needs internet):

   ```bash
   make audit
   ```

   Expected: the two warnings and `No known vulnerabilities found`, exit status 0.

2. See what a finding looks like, using an old version in a scratch file:

   ```bash
   printf 'jinja2==2.4.1\n' > /tmp/old.txt
   .venv/bin/pip-audit --no-deps --disable-pip -r /tmp/old.txt
   rm /tmp/old.txt
   ```

   Expected: a table with the columns `Name`, `Version`, `ID` and `Fix Versions`, one row per advisory for that very old Jinja2 (ids such as `PYSEC-2026-1471`, fixed in 3.1.x), and exit status 1 (`echo $?` right after the command).

3. Check the tool version: `.venv/bin/pip-audit --version` (this machine: `pip-audit 2.10.1`).

## Mistakes people make

- **Auditing the wrong thing.** Auditing loose requirements checks "whatever pip picks today". Audit the lock.
- **Ignoring a finding because the code "does not use that function".** Fix or document it; do not leave it.
- **Running it offline and reading silence as success.** It needs the network; an error is not "no vulnerabilities".
- **Assuming it finds everything.** It knows only reported vulnerabilities.

## Check yourself

1. What does pip-audit compare? *Each pinned package version against a database of known vulnerabilities.*
2. Why audit `requirements-dev.lock` and not `pyproject.toml`? *The lock has the exact versions that are installed.*
3. What does `--disable-pip` avoid? *Creating an environment and installing packages just to audit them.*
4. What should you do when it reports a finding? *Update to the fixed version, rerun tests, commit the new lock.*
5. Does a clean result prove the code is secure? *No, only that no known advisory matches these versions.*

## Further reading

- pip-audit (2.x): https://pypi.org/project/pip-audit/
- Source and options: https://github.com/pypa/pip-audit
- Python Packaging Advisory Database: https://github.com/pypa/advisory-database
