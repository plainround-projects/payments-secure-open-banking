# GitHub Actions

## What it is

GitHub Actions is GitHub's built-in **automation service** (continuous integration, CI). You describe **workflows** in YAML files under `.github/workflows/`. When something happens (a push, a pull request), GitHub starts a fresh virtual machine, checks out your code and runs the steps you listed. If a step fails, the workflow is marked failed and the change is flagged.

## Why this repository uses it

"It works on my machine" is not evidence. CI repeats the same commands on a clean machine after every push, so a missing file, an unpinned dependency or a test that only passes with local leftovers is caught. The workflow calls the same `make` targets a developer runs, so there is one definition of "checked".

## Where it appears

| Path | Role |
| --- | --- |
| `.github/workflows/ci.yml` | The only workflow (43 lines), two jobs. |

Nothing is pushed from this repository by the build task, so the workflow has not run on GitHub yet; it is validated by reading it and by running the same make targets locally.

## How it works underneath

An **event** triggers a **workflow**. A workflow has one or more **jobs**; jobs run in parallel on separate **runners** (virtual machines, here Ubuntu). A job is a list of **steps**; a step either runs a shell command (`run:`) or uses a reusable **action** (`uses:`, a packaged step published as `owner/name@version`). **Service containers** are Docker containers started beside the job, with the job able to reach them on `localhost`.

## Walkthrough of this repository's use

```yaml
name: ci

on: [push, pull_request]
```

`name` is the label in the GitHub UI. `on` lists the events: every push to any branch, and every pull request.

### Job 1: checks

```yaml
jobs:
  check:
    name: Format, lint, types, import rules, unit tests with coverage, dependency audit
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: make check
      - run: make audit
```

- `runs-on: ubuntu-latest`: the runner image.
- `actions/checkout@v4`: copies the repository onto the runner.
- `actions/setup-python@v5` with `python-version: "3.12"`: installs Python 3.12, the lowest version the project supports. (The version is quoted so YAML does not read `3.10` as the number 3.1.)
- `make check`: `make` first creates the virtual environment from the lock, then runs lint (Ruff, mypy, import-linter) and the unit tests with the 95 percent coverage bar.
- `make audit`: scans the locked dependencies for known vulnerabilities.

### Job 2: integration with a real database

```yaml
  integration:
    name: Integration and end-to-end tests (PostgreSQL service container)
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:18
        ports:
          - 54613:5432
        env:
          POSTGRES_USER: openbanking
          POSTGRES_PASSWORD: ci-only-throwaway
          POSTGRES_DB: openbanking
        options: >-
          --health-cmd "pg_isready -h 127.0.0.1 -U openbanking -d openbanking"
          --health-interval 3s
          --health-timeout 5s
          --health-retries 20
    env:
      POSTGRES_PASSWORD: ci-only-throwaway
      BANK_PEPPER: ci-only-throwaway-pepper
      SCRYPT_N: "1024"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: make test-integration test-e2e
```

- `services.postgres`: GitHub starts `postgres:18` before the steps. `ports: - 54613:5432` publishes it on the runner's port 54613, the port the tests expect by default.
- `env` under the service creates the role and database. The password `ci-only-throwaway` is a throwaway value that exists only in this CI job's disposable database; it protects nothing. The second `env`, at job level, gives the tests the same password because settings are read from the environment. It also sets two values the bank needs: `BANK_PEPPER`, a throwaway secret that keys the bank's digests (a real deployment generates a random one, as `make up` does locally), and `SCRYPT_N: "1024"`, which makes the slow password-hashing function cheap for the end-to-end tests' server processes so they run quickly (the default is 16384; see `hashing-hmac-and-scrypt.md`).
- `options: >-` passes `docker run` flags to the container: a health check like the Compose one. `>-` is YAML's "folded" string: the following lines are joined with spaces into one string without a trailing newline. GitHub waits for the service to be healthy before running steps.
- `make test-integration test-e2e` runs both targets in one command. The `integration` job does not need `.env`, because the variables are already set.

<!-- listing:.github/workflows/ci.yml -->
### `.github/workflows/ci.yml` (43 lines)

```yaml
name: ci

on: [push, pull_request]

jobs:
```

`name: ci` is the label. `on: [push, pull_request]` runs the workflow for every push and every pull request. `jobs:` holds the independent jobs.

```yaml
  check:
    name: Format, lint, types, import rules, unit tests with coverage, dependency audit
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: make check
      - run: make audit
```

The first job, `check`, with a long `name` describing its checks. `runs-on: ubuntu-latest` picks the machine image. `steps` run in order: `actions/checkout@v4` copies the repository onto the machine; `actions/setup-python@v5` with `python-version: "3.12"` installs Python (quoted so YAML does not read it as a number); `run: make check` runs lint and the unit tests with the coverage bar; `run: make audit` scans the dependencies.

```yaml
  integration:
    name: Integration and end-to-end tests (PostgreSQL service container)
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:18
        ports:
          - 54613:5432
        env:
          POSTGRES_USER: openbanking
          POSTGRES_PASSWORD: ci-only-throwaway
          POSTGRES_DB: openbanking
        options: >-
          --health-cmd "pg_isready -h 127.0.0.1 -U openbanking -d openbanking"
          --health-interval 3s
          --health-timeout 5s
          --health-retries 20
    env:
      POSTGRES_PASSWORD: ci-only-throwaway
      BANK_PEPPER: ci-only-throwaway-pepper
      SCRYPT_N: "1024"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: make test-integration test-e2e
```

The second job. `services: postgres:` starts a `postgres:18` container beside the job with `ports: - 54613:5432`, the role, password and database in `env` (named `openbanking`), and `options: >-` followed by health-check flags (`>-` folds the following lines into one string without a trailing newline). The job-level `env` gives the tests the settings they read from the environment: `POSTGRES_PASSWORD` (`ci-only-throwaway`, a throwaway for a disposable database), `BANK_PEPPER` (`ci-only-throwaway-pepper`, at least the required 16 characters) and `SCRYPT_N: "1024"` (a low password-hashing cost so CI is fast; the value is quoted so YAML keeps it as text). The steps check out the code, install Python and run `make test-integration test-e2e`.

<!-- /listing:.github/workflows/ci.yml -->

## Try it yourself

1. Read the same checks locally:

   ```bash
   make check && make audit
   ```

   Expected: lint passes (`All checks passed!`, `Success: no issues found`, all three import contracts kept), `198 passed`, coverage bar met, `No known vulnerabilities found`.

2. Check that the workflow file parses and see its structure (macOS includes Ruby, whose standard library reads YAML):

   ```bash
   ruby -ryaml -e 'y = YAML.load_file(".github/workflows/ci.yml"); p y["jobs"].keys; p y[true]'
   ```

   Expected: `["check", "integration"]` and `["push", "pull_request"]`. (The second value is the `on:` key: YAML 1.1 reads the bare word `on` as the boolean `true`, which is why it is printed as `y[true]`. GitHub understands it correctly.)

3. After pushing to GitHub, open the repository's **Actions** tab and see two jobs, `check` and `integration`.

## Mistakes people make

- **A workflow that does not use the project's own commands.** The two then drift apart. Here CI calls `make`.
- **Unquoted Python versions** (`3.10` becomes 3.1).
- **Floating action versions** (`@main`). Here the actions are pinned to a major version tag (`@v4`, `@v5`).
- **Real secrets in YAML.** Use GitHub Secrets for real credentials; the value here is a deliberate throwaway.
- **Forgetting health checks on service containers**, so tests start before the database accepts connections.

## Check yourself

1. What triggers this workflow? *Every push and every pull request.*
2. Why two jobs? *The checks need no services; integration and end-to-end tests need PostgreSQL. They run in parallel.*
3. What is a service container? *A Docker container started beside the job, reachable on localhost.*
4. Why is the password in the file acceptable? *It is a throwaway for a database that exists only during the job.*
5. Why does the workflow run `make check` and not each tool by hand? *One definition of the checks for developers and CI.*

## Further reading

- GitHub Actions documentation: https://docs.github.com/en/actions
- Workflow syntax: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
- Service containers: https://docs.github.com/en/actions/use-cases-and-examples/using-containerized-services/about-service-containers
- `actions/checkout`: https://github.com/actions/checkout
- `actions/setup-python`: https://github.com/actions/setup-python
