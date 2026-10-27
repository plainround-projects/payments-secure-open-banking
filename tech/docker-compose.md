# Docker Compose

## What it is

Docker Compose describes **several containers and how they connect** in one YAML file (`docker-compose.yml`) and starts them with one command (`docker compose up`). Each container is a **service**. Compose also creates a private network so services can reach each other by name. This repository uses Compose v2, the `docker compose` command that ships with Docker Desktop.

## Why this repository uses it

`make up` needs a PostgreSQL 18 database with a known user, password, port and health check. A compose file records those settings in one reviewable place instead of a long `docker run` command in a README. The same file can also run the API next to the database (the `app` profile).

## Where it appears

| Path | Role |
| --- | --- |
| `docker-compose.yml` | Two services: `postgres` (always) and `bank` (profile `app`), plus one named volume, `bank-data`. |
| `.env` (created from `.env.example` by `make up`) | Supplies `POSTGRES_PASSWORD` and optional overrides. Not committed. |
| `Makefile` | `up`, `down` and every `psql` call use `docker compose`. |

## How it works underneath

`docker compose up` reads the file, substitutes `${VARIABLES}` from the shell environment and `.env`, creates a network named after the project, creates each service's container, starts them in dependency order and (with `--wait`) waits for health checks. `exec` runs a command in an already-running service container. `down` stops and removes containers and the network; `-v` also removes volumes.

## Walkthrough of this repository's use

```yaml
name: payments-secure-open-banking

services:
```

`name` fixes the Compose **project name** (otherwise it comes from the folder name). Container names become `payments-secure-open-banking-postgres-1`. `services:` is the map of containers.

```yaml
postgres:
  image: postgres:18
  command: ["postgres", "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"]
```

- `image`: pull and run this image; no build.
- `command`: replaces the image's default command with `postgres` plus three `-c name=value` server settings. Each setting turns off a disk-safety step so tests run fast on a busy machine. A comment in the file says these are for a disposable database only.

```yaml
  ports:
    - "${POSTGRES_PORT:-54613}:5432"
```

`host:container`. `${POSTGRES_PORT:-54613}` means "the value of `POSTGRES_PORT`, or `54613` if unset or empty". The default is unusual on purpose, so it does not clash with a local PostgreSQL on 5432 or with other projects.

```yaml
  environment:
    POSTGRES_USER: ${POSTGRES_USER:-openbanking}
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?copy .env.example to .env first}
    POSTGRES_DB: ${POSTGRES_DB:-openbanking}
```

The official image reads these variables the first time it starts, creating the role and database. `${NAME:?message}` aborts `docker compose` with that message if `NAME` is unset or empty, so no password is ever written in the file or defaulted to something guessable.

```yaml
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
    interval: 5s
    timeout: 20s
    retries: 30
```

Every 5 seconds run the test (through a shell, so the variables expand inside the container; `$$` stops Compose expanding them itself). Each try may take 20 seconds. After 30 failed tries the service is `unhealthy`. `docker compose up --wait` waits for `healthy`.

```yaml
bank:
  profiles: ["app"]
  build: .
  image: payments-secure-open-banking:dev
  depends_on:
    postgres:
      condition: service_healthy
  ports:
    - "${BANK_PORT:-8463}:8463"
  environment:
    POSTGRES_HOST: postgres
    POSTGRES_PORT: "5432"
    ...
    BANK_PEPPER: ${BANK_PEPPER:?copy .env.example to .env first}
    BANK_HOST: 0.0.0.0
    BANK_PORT: "8463"
    BANK_PUBLIC_URL: http://localhost:${BANK_PORT:-8463}
    BANK_SIGNING_KEY_FILE: /data/bank-signing-key.pem
    OTP_OUTBOX_FILE: /data/otp-outbox.log
  volumes:
    - bank-data:/data
  command: ["open-banking-bank"]

volumes:
  bank-data:
```

- `profiles: ["app"]`: this service starts only when you ask (`--profile app`). Plain `make up` starts just the database.
- `build: .` builds the image from the `Dockerfile` in this folder; `image` names the result.
- `depends_on` with `condition: service_healthy`: start the bank only after the database is healthy.
- Inside the Compose network the bank reaches the database by the **service name** `postgres` on its container port `5432` (not on the host port 54613).
- `BANK_PEPPER` is a secret that keys the bank's digests of one-time codes and tokens. `:?` makes Compose refuse to start without it, and `make up` generates a random one into `.env` (see `shell.md`).
- `BANK_PUBLIC_URL` is the address the **merchant** uses for the bank. The bank needs it because a proof of possession is bound to the exact URL of the request (see `jwt-and-dpop.md`).
- `BANK_SIGNING_KEY_FILE` and `OTP_OUTBOX_FILE` point into `/data`.
- `volumes: - bank-data:/data` mounts a **named volume** at `/data`, and the top-level `volumes: bank-data:` declares it. A named volume is storage that Docker manages and that survives the container, so the bank keeps its signing key across restarts. `make down` runs `down -v`, which also deletes it.
- `command: ["open-banking-bank"]` states which program the container runs (it is also the image default).
- The `environment` list passes the settings the Python program reads (see `environment-variables-and-dotenv.md`).

## Try it yourself

1. Validate and print the final file with variables filled in:

   ```bash
   docker compose config
   ```

   Expected: the merged YAML; with no `.env` you get the message `copy .env.example to .env first`.

2. Start only the database: `make up`, then `docker compose ps` (the `bank` service does not appear).

3. Start both: `docker compose --profile app up --build -d`, then `docker compose ps` (two services) and `make health`.

4. Stop everything: `make down`.

## Mistakes people make

- **Putting passwords in the file.** Use `.env` (ignored by git) and the `:?` guard.
- **Single `$` in a health check command.** Compose would expand it from the host environment. Write `$$`.
- **Using the host port from another container.** Inside the network use the service name and the container port.
- **Assuming `depends_on` waits for readiness.** Only with `condition: service_healthy`.
- **Forgetting the profile.** The `bank` service silently does not start without `--profile app`.

## Check yourself

1. What does `${POSTGRES_PORT:-54613}` do? *Uses `POSTGRES_PORT`, or 54613 if it is unset.*
2. What does `${POSTGRES_PASSWORD:?message}` do when the variable is missing? *Stops Compose with the message.*
3. What is a profile for? *To start a service only on request.*
4. Why does the bank use `POSTGRES_HOST: postgres`? *Compose's network resolves the service name to the database container.*
5. What does `--wait` wait for? *The services' health checks to pass.*

## Further reading

- Compose file reference (Compose Specification): https://docs.docker.com/reference/compose-file/
- Services and `healthcheck`: https://docs.docker.com/reference/compose-file/services/
- Interpolation (`:-`, `:?`): https://docs.docker.com/reference/compose-file/interpolation/
- Profiles: https://docs.docker.com/compose/how-tos/profiles/
- `docker compose up --wait`: https://docs.docker.com/reference/cli/docker/compose/up/
