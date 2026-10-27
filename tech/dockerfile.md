# Dockerfile

## What it is

A `Dockerfile` is a text file of instructions that tells `docker build` how to assemble an **image**: start from a base image, copy files in, run commands, and say what the container should do when it starts. It is a file format with its own keywords, written in capitals (`FROM`, `COPY`, `RUN`). Each instruction that changes files makes one cached **layer**.

## Why this repository uses it

To package the bank and the merchant so they can run anywhere Docker runs, the way a deployment platform would run it: installed from the hash-pinned lock, as a non-root user, with a health check and a clean `SIGTERM` shutdown. The lesson does not require deployment, but a service you cannot package is hard to trust.

## Where it appears

| Path | Role |
| --- | --- |
| `Dockerfile` | The recipe (29 lines): one image holding two programs, the bank (the default command) and the merchant. |
| `.dockerignore` | What `COPY` may not see. |
| `Makefile` target `docker-build` | `docker build -t payments-secure-open-banking:dev .` |
| `docker-compose.yml`, service `api` | `build: .` builds from this file; used with `--profile app`. |

## How it works underneath

`docker build .` sends the **build context** (the folder, minus `.dockerignore` entries) to Docker. Docker runs the instructions top to bottom. For each, it checks the cache: if the instruction and the files it uses are unchanged since the last build, it reuses the stored layer; otherwise it runs it and every instruction after it again. So put things that change rarely first and things that change often last.

## Walkthrough of this repository's use

```dockerfile
FROM python:3.12.8-slim-bookworm@sha256:2199a628...
```

`FROM` picks the base image. `python:3.12.8-slim-bookworm` is the official Python 3.12.8 image on Debian 12 ("bookworm") in its slim variant (fewer packages, smaller). `@sha256:...` pins the exact content by digest, so even if the tag is re-published, this build is unchanged.

```dockerfile
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BANK_HOST=0.0.0.0 \
    BANK_PORT=8463
```

`ENV` sets environment variables for the build and for the running container. `\` continues the line.

- `PYTHONDONTWRITEBYTECODE=1`: do not write `.pyc` files (smaller image).
- `PYTHONUNBUFFERED=1`: send output straight to the log instead of holding it in a buffer, so log lines appear at once.
- `PIP_NO_CACHE_DIR=1`: pip does not keep a download cache in the image.
- `PIP_DISABLE_PIP_VERSION_CHECK=1`: pip does not call out to check for its own update.
- `BANK_HOST=0.0.0.0`: the bank listens on all network interfaces. Inside a container, `127.0.0.1` would be reachable only from inside it.
- `BANK_PORT=8463`: the port the bank listens on. (The merchant program uses `MERCHANT_HOST` and `MERCHANT_PORT`, default 8464, when you run it from this image.)

```dockerfile
WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock
```

- `WORKDIR` sets (and creates) the working folder for the following instructions.
- `COPY requirements.lock ./` copies one file from the build context into `/app`.
- `RUN` executes a command at build time and keeps the result. `--require-hashes` makes pip accept only the files whose hashes are in the lock.

The lock is copied and installed **before** the source. The install is the slow step; because the lock changes rarely, Docker reuses this layer when only code changed.

```dockerfile
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .
```

Now the project itself: `pyproject.toml` (and `README.md`, which it references as the long description) and the `src` folder. `pip install --no-deps .` installs this package and its command-line programs; `--no-deps` because the locked dependencies are already in.

```dockerfile
RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /data && chown app /data
USER app
VOLUME /data
```

- `useradd ...` creates a system user named `app` with a fixed numeric id and no home folder. A fixed numeric uid lets platforms that require "a non-root numeric user" verify it.
- `&& mkdir -p /data && chown app /data` (the same `RUN`, so one layer): while still root, create the folder where the bank keeps its **state files** and give it to `app`. The bank writes its signing key (`/data/bank-signing-key.pem`) and the file that stands in for the customer's phone (`/data/otp-outbox.log`) there.
- `USER app` switches to that user. Everything after, including the running service, is non-root.
- `VOLUME /data` declares that this folder holds data that must outlive a container. Compose mounts a named volume there (see `docker-compose.md`), so the bank keeps the same signing key after a restart.

```dockerfile
EXPOSE 8463
HEALTHCHECK --interval=15s --timeout=6s --retries=3 CMD ["open-banking-health"]
CMD ["open-banking-bank"]
```

- `EXPOSE` documents the port; it does not publish it (publishing is `-p` or Compose `ports`).
- `HEALTHCHECK`: every 15 seconds run `open-banking-health` (a program installed from `pyproject.toml`; it calls `/health`); fail it if it takes more than 6 seconds; mark the container `unhealthy` after 3 failures in a row.
- `CMD`: the default command, `open-banking-bank` (to run the merchant instead, override it: `open-banking-merchant`). The square-bracket form is the **exec form**: Docker runs the program directly, not through a shell, so it is process 1 and receives `SIGTERM` itself, which is what lets the server shut down gracefully.

## Try it yourself

1. Build it (needs Docker and network, can take minutes):

   ```bash
   make docker-build
   ```

   Expected: the last lines mention `naming to docker.io/library/payments-secure-open-banking:dev`.

2. Check the user and the configured check:

   ```bash
   docker run --rm --entrypoint id payments-secure-open-banking:dev
   docker inspect --format '{{json .Config.Healthcheck}}' payments-secure-open-banking:dev
   ```

   Expected: `uid=10001(app) gid=...` and a JSON object with `"Test":["CMD","open-banking-health"]`.

3. See layers: `docker history payments-secure-open-banking:dev`. Expected: one line per instruction with sizes; the `pip install` of the lock is the largest.

4. Run the bank with its database: `docker compose --profile app up --build`, then in another terminal `make health`.

## Mistakes people make

- **Copying everything first, then installing.** Any code edit then invalidates the install layer. Copy the lock, install, then copy the source.
- **Shell-form `CMD` (`CMD open-banking-bank`).** The shell becomes process 1 and may not pass `SIGTERM` on.
- **Binding to `127.0.0.1`** inside the container.
- **Running as root.**
- **Unpinned base image.** The bytes change under you.

## Check yourself

1. Why is the lock copied before `src`? *To reuse the cached dependency layer when only code changes.*
2. What does `USER app` change? *Later instructions and the running service no longer run as root.*
3. What does `EXPOSE` do? *Documents the port; it does not publish it.*
4. Why the exec form of `CMD`? *So the program is process 1 and gets signals directly.*
5. What does the digest after `@` guarantee? *The exact same base image content on every build.*

## Further reading

- Dockerfile reference: https://docs.docker.com/reference/dockerfile/
- Best practices for Dockerfiles: https://docs.docker.com/build/building/best-practices/
- Official Python image: https://hub.docker.com/_/python
