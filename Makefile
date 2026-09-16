SHELL := /bin/bash

# Local stand-in for CI. `make` / `make check` is the whole gate; `make help` lists the rest.
#
# `check` needs a Postgres for the integration lane -- `make test-db-up` starts a throwaway one.
# Targets that need the *network* stay out of it (`vuln` resolves the advisory database,
# `docker-build` wants a daemon), so the gate is local-only in the sense that matters: nothing
# in it reaches past this machine.
#
# Same contract as the sibling services aurea-highlights-api and aurea-feed-api: one target per
# concern, long comments on the ones that look odd, no lanes this tree does not have.

TEST_DB_URL ?= postgresql://postgres:test@localhost:55433/kwami_test
DOCKER_IMAGE ?= kwami-lk-api
DOCKER_TAG ?= dev

.DEFAULT_GOAL := check

.PHONY: help
help: ## List targets
	@awk 'BEGIN {FS = ":.*##"; printf "\n  make <target>\n\n"} \
		/^[a-zA-Z0-9_.-]+:.*?## / {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo

# `make` / `make check` is the whole gate, and it is the same set of checks ci.yml runs as
# separate jobs. The two it leaves out both need the network: `make vuln` and the image build.
.PHONY: check
check: lint fmt-check coverage coverage-gate migrate-dry-run ## Full gate (needs Postgres)

# =============================================================================
# Development
# =============================================================================

.PHONY: install
install: ## Sync the venv, dev extra included
	uv sync --extra dev

.PHONY: dev
dev: ## Run the API on :8080
	uv run python -m src.main

# Points git at .githooks/, whose pre-push refuses a direct push to main.
#
# It is a per-clone setting rather than something git picks up on its own -- hooks are
# deliberately not transferable -- so every clone runs this once. It is the local half of the
# rule; `make rules` applies the server-side one, which is the control. This repository is
# public, so that ruleset does apply here -- see .github/rulesets/main.json.
.PHONY: hooks
hooks: ## Point git at .githooks/ (refuses a direct push to main)
	git config core.hooksPath .githooks
	@echo "hooks installed: pushing straight to main will now be refused"

.PHONY: rules
rules: ## Apply .github/rulesets/ to GitHub (needs gh, admin rights)
	./scripts/branch-protection.sh

# =============================================================================
# Tests
# =============================================================================

# `--extra dev` is required: the test tooling lives in an optional extra, so a plain `uv sync`
# leaves the venv without pytest.
.PHONY: test
test: ## Unit and API tests (no database, no network)
	uv run --extra dev pytest -m "not integration and not e2e"

.PHONY: test-cov
test-cov: ## The unit lane with a coverage report
	uv run --extra dev pytest -m "not integration and not e2e" \
		--cov=src --cov-report=term-missing

# The money invariants are not in Python: `UPDATE ... WHERE balance >= p_amount` in a single
# statement, partial unique indexes, ON CONFLICT DO NOTHING, RLS policies. They need a real
# Postgres with migrations/*.sql applied, which is what this lane is.
#
# The tests SKIP when TEST_DATABASE_URL is unset -- that is what keeps `make test` offline -- so
# this target always sets it, and a connection failure is a failure rather than a quiet pass.
.PHONY: test-integration
test-integration: ## Migrations and money invariants (needs Postgres)
	@TEST_DATABASE_URL=$${TEST_DATABASE_URL:-$(TEST_DB_URL)} \
		uv run --extra dev --extra integration pytest -m integration \
		|| { echo; echo "hint: 'make test-db-up' starts a throwaway Postgres on 55433"; exit 1; }

.PHONY: test-all
test-all: ## Every marker, one run
	@TEST_DATABASE_URL=$${TEST_DATABASE_URL:-$(TEST_DB_URL)} \
		uv run --extra dev --extra integration pytest

.PHONY: test-db-up
test-db-up: ## Start a throwaway Postgres on 55433
	docker rm -f kwami-test-pg 2>/dev/null || true
	docker run -d --name kwami-test-pg \
		-e POSTGRES_PASSWORD=test -e POSTGRES_DB=kwami_test \
		-p 55433:5432 postgres:16-alpine
	@until docker exec kwami-test-pg pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready on 55433"

.PHONY: test-db-down
test-db-down: ## Remove the throwaway Postgres
	docker rm -f kwami-test-pg

# =============================================================================
# Coverage
# =============================================================================

# Both lanes, merged into one profile -- the same thing ci.yml does across two jobs and an
# artifact. They cannot share a run: `pytest --cov` writes `.coverage` at the end of the
# session, so the second lane would overwrite the first. Each is staged under its own name and
# `coverage combine` sums them.
#
# `parallel` and `relative_files` in pyproject.toml are what make the merge work at all, here
# and across two runners in CI.
.PHONY: coverage
coverage: ## Run both lanes and merge their profiles (needs Postgres)
	rm -f .coverage .coverage.* coverage.unit coverage.integration coverage.json
	uv run --extra dev pytest -m "not integration and not e2e" --cov=src --cov-report=
	mv .coverage coverage.unit
	@TEST_DATABASE_URL=$${TEST_DATABASE_URL:-$(TEST_DB_URL)} \
		uv run --extra dev --extra integration pytest -m integration --cov=src --cov-report= \
		|| { echo; echo "hint: 'make test-db-up' starts a throwaway Postgres on 55433"; exit 1; }
	mv .coverage coverage.integration
	uv run --extra dev coverage combine coverage.unit coverage.integration
	uv run --extra dev coverage report

# Fails on a module below its floor AND on a module listed in neither the floors nor the
# exclusions -- a new module must be given a coverage decision on purpose. Reads whatever
# `.coverage` currently holds, so it gates a single lane or a merged profile alike.
.PHONY: coverage-gate
coverage-gate: ## Enforce coverage.floors against the current profile
	uv run --extra dev coverage json -o coverage.json -q
	uv run python scripts/coverage_gate.py coverage.json coverage.floors

.PHONY: coverage-html
coverage-html: ## Write htmlcov/ from the current profile
	uv run --extra dev coverage html
	@echo "open htmlcov/index.html"

# =============================================================================
# Code quality
# =============================================================================

.PHONY: lint
lint: ## ruff check
	uv run --extra dev ruff check .

.PHONY: fmt
fmt: format
.PHONY: format
format: ## ruff format + ruff check --fix
	uv run --extra dev ruff format .
	uv run --extra dev ruff check --fix .

.PHONY: fmt-check
fmt-check: ## Fail if ruff format would rewrite a file
	uv run --extra dev ruff format --check .

# Every advisory against the resolved dependency set. pip-audit does no reachability analysis,
# so this is noisier than the govulncheck the Go services gate on: it is run in CI as an
# ADVISORY job, not a required check, until the open advisories are cleared. See ci.yml.
#
# Deliberately not part of `check`: it resolves the advisory database over the network, and the
# default gate has to stay runnable offline.
#
# `--no-deps` audits exactly the versions uv.lock pins, with no resolution of its own; without
# it pip-audit would re-resolve and report on versions this project does not install.
.PHONY: vuln
vuln: ## pip-audit over the locked dependency set (network; not part of check)
	uv export --frozen --no-emit-project --no-hashes --format requirements-txt -o "$${TMPDIR:-/tmp}/kwami-requirements.txt"
	uvx pip-audit --no-deps -r "$${TMPDIR:-/tmp}/kwami-requirements.txt"

# =============================================================================
# Database
# =============================================================================

.PHONY: migrate
migrate: ## Apply pending migrations (needs DATABASE_URL)
	uv run --extra integration python scripts/migrate.py

.PHONY: migrate-dry-run
migrate-dry-run: ## List pending migrations, and refuse duplicate version prefixes
	uv run python scripts/migrate.py --dry-run

# Run ONCE against a database that was migrated by hand, to record history without re-running it.
.PHONY: migrate-baseline
migrate-baseline: ## Record migrations up to 010 as already applied
	uv run --extra integration python scripts/migrate.py --baseline 010

.PHONY: migrate-verify
migrate-verify: ## Fail if an applied migration file has been edited since it ran
	uv run --extra integration python scripts/migrate.py --verify

# =============================================================================
# Release
# =============================================================================

# Writes one version into pyproject.toml, uv.lock, src/__init__.py and the README badge.
# semantic-release calls this from .releaserc.json; by hand it is only for a repair.
.PHONY: set-version
set-version: ## VERSION=1.2.3 make set-version
	@[ -n "$(VERSION)" ] || { echo "usage: VERSION=1.2.3 make set-version" >&2; exit 1; }
	./scripts/set-version.sh $(VERSION)

# =============================================================================
# Docker and deploy
# =============================================================================

.PHONY: docker-build
docker-build: ## Build the release image CD publishes
	docker build -f Dockerfile -t $(DOCKER_IMAGE):$(DOCKER_TAG) .

.PHONY: docker-up
docker-up: ## Run that image on :8080 with .env
	docker run -d --name kwami-lk-api -p 8080:8080 --env-file .env $(DOCKER_IMAGE):$(DOCKER_TAG)

.PHONY: docker-down
docker-down: ## Stop and remove it
	docker stop kwami-lk-api && docker rm kwami-lk-api

# CD deploys on every green push to main; this is the manual escape hatch, not the usual path.
.PHONY: deploy
deploy: ## fly deploy (CD normally does this)
	fly deploy --remote-only

# =============================================================================
# Cleanup
# =============================================================================

.PHONY: clean
clean: ## Remove caches and coverage artefacts
	rm -rf __pycache__ **/__pycache__ .pytest_cache .ruff_cache htmlcov
	rm -f .coverage .coverage.* coverage.unit coverage.integration coverage.json coverage.xml
