# make up | db | run | serve-bank | serve-merchant | queries | test | test-integration | test-e2e | lint | audit | down   (make help lists all)
PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3)
VENV := .venv
PY := $(VENV)/bin/python
STAMP := $(VENV)/.installed
POSTGRES_USER ?= openbanking
POSTGRES_DB ?= openbanking
PSQL := docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -q

-include .env
export

.PHONY: help install lock env up db reset-db migrate queries run register serve-bank serve-merchant health lint format test \
        test-integration test-e2e audit check docker-build down clean

help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort

$(STAMP): requirements-dev.lock pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q --require-hashes -r requirements-dev.lock
	$(VENV)/bin/pip install -q --no-deps -e .
	touch $@

install: $(STAMP) ## create .venv from the lock file

lock: ## regenerate the hash-pinned lock files from pyproject.toml (needs uv)
	uv pip compile pyproject.toml --generate-hashes --python-version 3.12 -o requirements.lock
	uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 -o requirements-dev.lock

# .env is created from .env.example; the pepper that keys the bank's digests is generated here, never committed.
env:
	@[ -f .env ] || { cp .env.example .env; \
	  pepper=$$(openssl rand -hex 24); sed -i.bak "s/^BANK_PEPPER=.*/BANK_PEPPER=$$pepper/" .env && rm -f .env.bak; \
	  echo "created .env from .env.example with a generated BANK_PEPPER"; }

up: env $(STAMP) ## start PostgreSQL and load sql/001_schema.sql and sql/002_seed.sql
	docker compose up -d --wait postgres
	$(MAKE) --no-print-directory db

reset-db:
	$(PSQL) < sql/reset.sql

db: reset-db ## recreate the schema from sql/001_schema.sql and load sql/002_seed.sql (drops existing data)
	$(PSQL) < sql/001_schema.sql
	$(PSQL) < sql/002_seed.sql

# The lesson's stepwise path: apply sql/migrations/*.sql, in order, to a scratch database (openbanking_migrated).
migrate: ## apply sql/migrations/*.sql to a scratch database and list its tables
	-docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -q -c "DROP DATABASE IF EXISTS openbanking_migrated"
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -q -c "CREATE DATABASE openbanking_migrated"
	@for file in sql/migrations/*.sql; do echo "applying $$file"; \
	  docker compose exec -T postgres psql -U $(POSTGRES_USER) -d openbanking_migrated -v ON_ERROR_STOP=1 -q < $$file; done
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d openbanking_migrated -c "\dt"

queries: ## run sql/003_queries.sql
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 < sql/003_queries.sql

# The lesson's whole scenario with a bank and a merchant server in this process: consent, refusals, replay, tampering,
# abandonment, revocation, a lost response, and the stretch goals. Resets the database first.
run: $(STAMP) db ## the lesson's scenario end to end, including the failures (resets the database)
	rm -rf var
	$(VENV)/bin/open-banking-demo
	$(MAKE) --no-print-directory queries

# Register the demo merchant and customers by hand, to run the bank and merchant as separate processes.
register: $(STAMP) ## register merchant-1 and enroll ada and bob (writes secrets under var/, git-ignored)
	$(VENV)/bin/open-banking-admin register-merchant --client-id merchant-1 --name "Shop"
	$(VENV)/bin/open-banking-admin enroll-customer --username ada --account acct-ada
	$(VENV)/bin/open-banking-admin enroll-customer --username bob --account acct-bob

serve-bank: $(STAMP) ## run the bank API in the foreground (Ctrl-C or SIGTERM stops it gracefully)
	$(VENV)/bin/open-banking-bank

serve-merchant: $(STAMP) ## run the merchant API in the foreground (needs make register first)
	set -a; . var/merchant-1.env; set +a; $(VENV)/bin/open-banking-merchant

health: $(STAMP) ## exit 0 when the running bank reports healthy
	$(VENV)/bin/open-banking-health

lint: $(STAMP) ## ruff check, ruff format check, mypy, import-linter
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy
	$(VENV)/bin/lint-imports

format: $(STAMP) ## apply ruff's formatting and safe fixes
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .

# Unit tests need no services. The domain and application must stay at 95% coverage or more.
test: $(STAMP) ## unit tests with coverage (no database needed)
	$(PY) -m pytest tests/unit --cov=open_banking --cov-report=term
	$(PY) -m coverage report --include='src/open_banking/domain/*,src/open_banking/application/*' --fail-under=95

test-integration: $(STAMP) ## adapters and the lesson's rules against the running database (make up first)
	$(PY) -m pytest tests/integration -m integration

test-e2e: $(STAMP) ## the scenario against real bank and merchant server processes (make up first)
	$(PY) -m pytest tests/e2e -m e2e

audit: $(STAMP) ## check the locked dependencies for known vulnerabilities
	$(VENV)/bin/pip-audit --no-deps --disable-pip -r requirements-dev.lock

check: lint test ## everything that needs no services

docker-build: ## build the container image of the bank and the merchant
	docker build -t payments-secure-open-banking:dev .

down: ## stop PostgreSQL and delete its data
	docker compose --profile app down -v

clean: down
	rm -rf $(VENV) logs var .pytest_cache .mypy_cache .ruff_cache .hypothesis .coverage
