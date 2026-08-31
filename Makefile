.PHONY: help install dev up down status doctor lint format format-check fix test test-file cov check build clean \
        db-up db-down db-upgrade db-migrate db-history db-current db-downgrade

.DEFAULT_GOAL := help

ALL_DIRS := src/ tests/

help: ## Show available commands
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

# ─── Setup ───────────────────────────────────────────────

install: ## Install all dependencies
	uv sync --all-extras

clean: ## Remove generated artifacts
	rm -rf dist/ build/ *.egg-info .pytest_cache .coverage coverage_html .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ─── Run ─────────────────────────────────────────────────

dev: ## Run the backbone with auto-reload
	uv run backbone up --reload

up: ## Run the backbone detached in a tmux session
	uv run backbone up --detach

down: ## Stop the detached backbone
	uv run backbone down

status: ## Show agents, sessions and service health
	uv run backbone status

doctor: ## Check environment and configuration
	uv run backbone doctor

# ─── Code Quality ────────────────────────────────────────

lint: ## Run linter
	uv run ruff check $(ALL_DIRS)

format: ## Format code
	uv run ruff format $(ALL_DIRS)

format-check: ## Check formatting (no changes)
	uv run ruff format --check $(ALL_DIRS)

fix: ## Auto-fix lint issues + format
	uv run ruff check --fix $(ALL_DIRS)
	uv run ruff format $(ALL_DIRS)

# ─── Testing ─────────────────────────────────────────────

test: ## Run all tests (SQLite in-memory, no services needed)
	uv run pytest

test-file: ## Run a single test file (FILE=tests/unit/test_foo.py)
	uv run pytest $(FILE) -v

cov: ## Run tests with coverage
	uv run pytest --cov=src --cov-report=term-missing

check: lint format-check test ## Full quality check (CI equivalent)

build: ## Build wheel + sdist
	uv build

# ─── Database (optional PostgreSQL) ──────────────────────

db-up: ## Start PostgreSQL via docker compose (optional; SQLite is the default)
	docker compose up -d

db-down: ## Stop PostgreSQL
	docker compose down

db-upgrade: ## Run Alembic migrations to head
	uv run alembic upgrade head

db-migrate: ## Create a new migration (MSG="description")
	uv run alembic revision --autogenerate -m "$(MSG)"

db-history: ## Show migration history
	uv run alembic history

db-current: ## Show current migration version
	uv run alembic current

db-downgrade: ## Downgrade one migration step
	uv run alembic downgrade -1
