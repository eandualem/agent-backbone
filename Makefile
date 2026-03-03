.PHONY: help install dev logs clean lint format format-check fix type-check \
       test test-file test-unit test-integration cov cov-html check build \
       run-gateway run-prefect setup-pool deploy run-worker \
       db-up db-down db-upgrade db-migrate db-revision db-history db-current db-downgrade \
       start-backbone stop-backbone restart-backbone start-tunnel stop-tunnel infra-status

.DEFAULT_GOAL := help

# ─── Config ──────────────────────────────────────────────

PROJECT_NAME := agent-backbone
SRC_DIRS     := src/
ALL_DIRS     := src/ tests/

# ─── Colors ──────────────────────────────────────────────

GREEN  := $(shell tput -Txterm setaf 2)
YELLOW := $(shell tput -Txterm setaf 3)
CYAN   := $(shell tput -Txterm setaf 6)
RED    := $(shell tput -Txterm setaf 1)
RESET  := $(shell tput -Txterm sgr0)

# ─── Help ────────────────────────────────────────────────

help: ## Show available commands
	@echo ""
	@echo "$(CYAN)$(PROJECT_NAME)$(RESET) — Development Commands"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"; printf "Usage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

# ─── Setup ───────────────────────────────────────────────

install: ## Install all dependencies
	@echo "$(CYAN)Installing dependencies...$(RESET)"
	uv sync
	@echo "$(GREEN)Done.$(RESET)"

clean: ## Remove generated artifacts
	@echo "$(CYAN)Cleaning...$(RESET)"
	rm -rf dist/ build/ *.egg-info .pytest_cache .coverage coverage_html .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "$(GREEN)Done.$(RESET)"

# ─── Development ─────────────────────────────────────────

dev: ## Restart gateway with latest code (auto-reload enabled)
	@if tmux has-session -t gateway 2>/dev/null; then \
		echo "$(CYAN)Restarting gateway service...$(RESET)"; \
		uv run python -m agent_backbone.services.infrastructure restart-gateway; \
	else \
		echo "$(CYAN)Starting gateway service...$(RESET)"; \
		uv run python -m agent_backbone.services.infrastructure start-gateway; \
	fi
	@echo "$(GREEN)Gateway running in tmux session 'gateway' — attach with: tmux attach -t gateway$(RESET)"

logs: ## Tail gateway logs (attach to tmux session)
	@tmux attach -t gateway 2>/dev/null || echo "Gateway not running. Start with: make dev"

# ─── Code Quality ────────────────────────────────────────

lint: ## Run linter
	@echo "$(CYAN)Linting...$(RESET)"
	uv run ruff check $(ALL_DIRS)
	@echo "$(GREEN)Done.$(RESET)"

format: ## Format code
	@echo "$(CYAN)Formatting...$(RESET)"
	uv run ruff format $(ALL_DIRS)
	@echo "$(GREEN)Done.$(RESET)"

format-check: ## Check formatting (no changes)
	@echo "$(CYAN)Checking format...$(RESET)"
	uv run ruff format --check $(ALL_DIRS)
	@echo "$(GREEN)Done.$(RESET)"

fix: ## Auto-fix lint issues + format
	@echo "$(CYAN)Fixing...$(RESET)"
	uv run ruff check --fix $(ALL_DIRS)
	uv run ruff format $(ALL_DIRS)
	@echo "$(GREEN)Done.$(RESET)"

type-check: ## Run type checker
	@echo "$(CYAN)Type checking...$(RESET)"
	uv run pyright $(SRC_DIRS)
	@echo "$(GREEN)Done.$(RESET)"

# ─── Testing ─────────────────────────────────────────────

test: ## Run all tests
	@echo "$(CYAN)Running tests...$(RESET)"
	uv run pytest
	@echo "$(GREEN)Done.$(RESET)"

test-file: ## Run a single test file (FILE=tests/test_foo.py)
	uv run pytest $(FILE) -v

test-unit: ## Run unit tests only
	@echo "$(CYAN)Running unit tests...$(RESET)"
	uv run pytest tests/unit/
	@echo "$(GREEN)Done.$(RESET)"

test-integration: ## Run integration tests only
	@echo "$(CYAN)Running integration tests...$(RESET)"
	uv run pytest tests/integration/
	@echo "$(GREEN)Done.$(RESET)"

cov: ## Run tests with coverage
	@echo "$(CYAN)Running tests with coverage...$(RESET)"
	uv run pytest --cov=src --cov-report=term-missing
	@echo "$(GREEN)Done.$(RESET)"

cov-html: ## Generate HTML coverage report
	@echo "$(CYAN)Generating HTML coverage...$(RESET)"
	uv run pytest --cov=src --cov-report=html:coverage_html
	@echo "$(GREEN)Report at coverage_html/index.html$(RESET)"

# ─── Quality Gate ────────────────────────────────────────

check: lint format-check test ## Full quality check (CI equivalent)
	@echo "$(GREEN)All checks passed.$(RESET)"

# ─── Build ───────────────────────────────────────────────

build: ## Production build
	@echo "$(CYAN)Building...$(RESET)"
	uv build
	@echo "$(GREEN)Done.$(RESET)"

# ─── Services ────────────────────────────────────────────

run-gateway: ## Start gateway server (prefer 'make dev' for auto-reload)
	@uv run python -m agent_backbone.services.infrastructure start-gateway

run-prefect: ## Start Prefect server (port 4200)
	uv run prefect server start

setup-pool: ## Create agent-pool work pool (one-time)
	uv run prefect work-pool create agent-pool --type process

deploy: ## Deploy all scheduled flows
	uv run prefect deploy --all

run-worker: ## Start Prefect worker for agent-pool
	uv run prefect worker start --pool agent-pool

# ─── Database ───────────────────────────────────────────

db-up: ## Start PostgreSQL (docker-compose)
	@echo "$(CYAN)Starting PostgreSQL...$(RESET)"
	docker compose up -d
	@echo "$(GREEN)PostgreSQL running on port 5435.$(RESET)"

db-down: ## Stop PostgreSQL (docker-compose)
	@echo "$(CYAN)Stopping PostgreSQL...$(RESET)"
	docker compose down
	@echo "$(GREEN)Done.$(RESET)"

db-upgrade: ## Run Alembic migrations to head
	uv run alembic upgrade head

db-migrate: ## Create a new migration (MSG="description")
	uv run alembic revision --autogenerate -m "$(MSG)"

db-revision: ## Create a new migration (MSG="description") — alias for db-migrate
	uv run alembic revision --autogenerate -m "$(MSG)"

db-history: ## Show migration history
	uv run alembic history

db-current: ## Show current migration version
	uv run alembic current

db-downgrade: ## Downgrade one migration step
	uv run alembic downgrade -1

# ─── Infrastructure Management ──────────────────────────

start-backbone: ## Start all services (Prefect + Gateway + Worker + Telegram)
	@uv run python -m agent_backbone.services.infrastructure start-backbone

stop-backbone: ## Stop all services
	@uv run python -m agent_backbone.services.infrastructure stop-backbone

restart-backbone: ## Restart all services
	@uv run python -m agent_backbone.services.infrastructure restart-backbone

start-tunnel: ## Start ngrok tunnel
	@uv run python -m agent_backbone.services.infrastructure start-tunnel

stop-tunnel: ## Stop ngrok tunnel
	@uv run python -m agent_backbone.services.infrastructure stop-tunnel

infra-status: ## Show all services and sessions
	@uv run python -m agent_backbone.services.infrastructure status
