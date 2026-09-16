.PHONY: help install dev test lint format clean docker-build docker-up docker-down deploy

help:
	@echo "Kwami AI API - Development Commands"
	@echo ""
	@echo "Development:"
	@echo "  make install       - Install dependencies"
	@echo "  make dev           - Run API server (dev mode)"
	@echo "  make test          - Run unit and API tests"
	@echo "  make test-cov      - Run tests with coverage"
	@echo "  make test-integration - Run integration tests (needs Postgres)"
	@echo ""
	@echo "Database:"
	@echo "  make migrate       - Apply pending migrations"
	@echo "  make migrate-dry-run  - List pending migrations"
	@echo ""
	@echo "Code Quality:"
	@echo "  make lint          - Run linter"
	@echo "  make format        - Format code"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-build  - Build Docker image"
	@echo "  make docker-up     - Start container"
	@echo "  make docker-down   - Stop container"
	@echo ""
	@echo "Deploy:"
	@echo "  make deploy        - Deploy to Fly.io (fly deploy)"

# =============================================================================
# Development
# =============================================================================

install:
	uv sync --extra dev

dev:
	uv run python -m src.main

# `--extra dev` is required: the test tooling lives in an optional extra, so a
# plain `uv sync` leaves the venv without pytest.
test:
	uv run --extra dev pytest -m "not integration and not e2e"

test-cov:
	uv run --extra dev pytest -m "not integration and not e2e" \
		--cov=src --cov-report=term-missing --cov-report=xml

test-integration:
	uv run --extra dev --extra integration pytest -m integration

test-all:
	uv run --extra dev --extra integration pytest

# =============================================================================
# Code Quality
# =============================================================================

lint:
	uv run ruff check .

format:
	uv run ruff format . && uv run ruff check --fix .

# =============================================================================
# Database
# =============================================================================

# Apply pending migrations. Requires DATABASE_URL.
migrate:
	uv run --extra integration python scripts/migrate.py

migrate-dry-run:
	uv run python scripts/migrate.py --dry-run

# Run ONCE against a database that was migrated by hand, to record history
# without re-running it.
migrate-baseline:
	uv run --extra integration python scripts/migrate.py --baseline 010

# Fail if an already-applied migration file has been edited since it ran.
migrate-verify:
	uv run --extra integration python scripts/migrate.py --verify

# =============================================================================
# Docker
# =============================================================================

docker-build:
	docker build -t kwami-lk-api .

docker-up:
	docker run -d --name kwami-lk-api -p 8080:8080 --env-file .env kwami-lk-api

docker-down:
	docker stop kwami-lk-api && docker rm kwami-lk-api

# =============================================================================
# Deploy
# =============================================================================

deploy:
	fly deploy

# =============================================================================
# Cleanup
# =============================================================================

clean:
	rm -rf .venv __pycache__ **/__pycache__
	rm -rf .pytest_cache .ruff_cache
