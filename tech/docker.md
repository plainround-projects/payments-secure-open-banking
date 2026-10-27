# Docker

## What it is

Docker runs programs in **containers**: isolated processes that see their own file system, their own network and their own process list, while sharing the computer's kernel. A container is started from an **image**, a read-only package holding a program and everything it needs (files, libraries, settings). Think of the image as the recipe-and-ingredients box and the container as one running meal. Docker Desktop (on macOS) runs a small Linux virtual machine that hosts the containers.

## Why this repository uses it

Two reasons. First, the lesson's database should be one command away and the same for everyone: `make up` starts PostgreSQL 18 from the official image without installing anything on the computer. Second, the bank and the merchant share one `Dockerfile`, so they can be built into an image and run the way a deployment platform would run it: as a non-root user, with a health check, shutting down on `SIGTERM`.

## Where it appears

| Path | Role |
| --- | --- |
| `docker-compose.yml` | Describes the PostgreSQL container, and the bank container under the `app` profile (see `docker-compose.md`). |
| `Dockerfile` | Recipe for the image that holds the bank and the merchant (see `dockerfile.md`). |
| `.dockerignore` | Files that must not be sent to the image build. |
| `Makefile` | `up`, `down` and `docker-build` call `docker compose` and `docker build`; the `PSQL` variable runs `psql` inside the container with `docker compose exec`. |
| `.github/workflows/ci.yml` | Starts `postgres:18` as a CI service container. |

## How it works underneath

- **Image layers.** An image is a stack of read-only layers. Each instruction in a `Dockerfile` that changes files adds a layer. Docker caches layers, so a rebuild reuses everything above the first changed instruction.
- **Registry.** Images are stored in registries (Docker Hub is the default). `postgres:18` means repository `postgres`, tag `18`; Docker pulls it the first time.
- **Container = process + isolation.** Linux features called namespaces (separate views of processes, network, file system) and cgroups (resource limits) isolate it. When the main process exits, the container stops.
- **Writable layer and volumes.** A container writes into a thin layer that disappears with the container. Data that must outlive a container goes in a **volume**. This repository does not use a volume for PostgreSQL (only the bank's optional container has a named volume, `bank-data`, for its signing key and the one-time-code outbox; see `docker-compose.md`): the data is meant to be thrown away, and `make down` runs `docker compose down -v` (`-v` also removes volumes).
- **Ports.** A container has its own network. `"54613:5432"` publishes the container's port 5432 on the computer's port 54613.
- **Health checks.** Docker can run a command inside the container on a timer and mark it `healthy` or `unhealthy`.

## Walkthrough of this repository's use

The commands you will see:

| Command | Meaning |
| --- | --- |
| `docker compose up -d --wait postgres` | Create and start the `postgres` service in the background (`-d`), and do not return until its health check passes (`--wait`). |
| `docker compose exec -T postgres psql ...` | Run `psql` inside the running container. `-T` skips allocating a terminal, needed when input is redirected from a file. |
| `docker compose --profile app down -v` | Stop and remove the containers and network, including the bank service if it ran, and delete volumes. |
| `docker build -t payments-secure-open-banking:dev .` | Build the image from the `Dockerfile` in the current folder (`.`), naming it with `-t` (name:tag). |

Why the database image is not rebuilt: `postgres:18` is used as published. Only the bank/merchant image is built from this repository.

Why `docker compose exec` into the container for SQL: `psql` is already in the PostgreSQL image, so the computer needs no PostgreSQL client installed.

### The repository's safety choices

- The image runs as an unprivileged user (`USER app`, uid 10001), so a bug in the bank cannot act as root inside the container.
- The base image is pinned by tag **and digest**, so a rebuild cannot silently pick up different bytes.
- `.dockerignore` keeps `.git`, `.venv`, `tests`, `docs` and `.env` out of the build, so the image stays small and no secret in `.env` can be copied in.

## Try it yourself

1. Check Docker is running:

   ```bash
   docker version --format '{{.Server.Version}}'
   ```

   Expected: a version number (the engine's version). If it errors, start Docker Desktop.

2. Start the database and list containers:

   ```bash
   make up
   docker compose ps
   ```

   Expected: one service `postgres`, state `running` and status `healthy`, port mapping `0.0.0.0:54613->5432/tcp`.

3. Look inside the container:

   ```bash
   docker compose exec postgres sh -c 'whoami; psql --version'
   ```

   Expected: `root` and `psql (PostgreSQL) 18.x`.

4. Remove everything: `make down`. Then `docker compose ps` shows nothing.

5. Build the image (needs time and network): `make docker-build`, then `docker image ls payments-secure-open-banking`.

## Mistakes people make

- **Storing important data in a container's writable layer or a throwaway volume** and losing it with `down -v`. Here that is intended; for real data use a named volume and do not pass `-v`.
- **Using `latest` or an unpinned tag.** The tag may move. This repository pins `postgres:18` (major) and a full digest for Python.
- **Running as root** inside the container.
- **Publishing a port that is already taken.** Hence the unusual port 54613.
- **Copying secrets into an image.** `.dockerignore` and passing passwords by environment variable at run time avoid it.

## Check yourself

1. What is the difference between an image and a container? *An image is the read-only package; a container is a running instance of it.*
2. Why does `docker compose up --wait` matter for `make up`? *The schema is loaded right after; the database must be ready first.*
3. What does `"54613:5432"` mean? *Computer port 54613 forwards to container port 5432.*
4. Why does `make down` pass `-v`? *To delete volumes so the next start is a clean, empty database.*
5. Why pin an image by digest? *So the exact same bytes are used on every build.*

## Further reading

- Docker documentation (Engine 28, Compose v2): https://docs.docker.com/
- Docker overview: https://docs.docker.com/get-started/docker-overview/
- Image layers and caching: https://docs.docker.com/build/cache/
- The official PostgreSQL image: https://hub.docker.com/_/postgres
