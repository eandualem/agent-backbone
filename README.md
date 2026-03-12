# Agent Backbone

Webhook processing pipeline and orchestration infrastructure for the multi-agent workspace. Receives GitHub webhook events from `eandualem/orchestration`, routes them to the right agent tmux sessions, manages delivery lifecycle (retry, dedup, close-then-next), and exposes a REST API with real-time streaming for a dashboard frontend.

## Quick Start

```bash
# 1. Start PostgreSQL
make db-up

# 2. Run database migrations
make db-upgrade

# 3. Configure environment
cp .env.example .env
# Edit .env: set GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY_PATH,
# GITHUB_APP_WEBHOOK_SECRET, TELEGRAM_TOKEN

# 4. Install dependencies
make install

# 5. Start the gateway
make dev
```

## Architecture

```
GitHub webhook --> FastAPI (api/app.py) --> Prefect flows --> tmux / Jarvis
                        |                        |
                  LifecycleManager         services/_locator.py
                  (ordered start/stop)     (service locator)
                        |                        |
                  14 services on           scheduled flows use
                  app.state                locator getters
                        |
                  REST API  <-- Socket.IO (PTY terminals)
                  SSE streaming    WebSocket attach-session
                        |
                  Telegram bot -- mobile control
                        |
                  PostgreSQL (port 5435) -- persistent state
```

## Services

14 services under `src/agent_backbone/services/`, each following the pattern: `interface.py` + `factory.py` + `exceptions.py`.

| # | Service | Purpose |
|---|---------|---------|
| 1 | `database` | PostgreSQL engine lifecycle, connection pooling, ORM models, session factory |
| 2 | `persistence` | Domain-specific query repositories (delivery, state, queue, dedup) |
| 3 | `registry` | Entity/repo registry from JSON + filesystem discovery |
| 4 | `github` | GitHub REST API client (httpx, async) |
| 5 | `tmux` | Async tmux operations (sessions, panes, windows, key sending) |
| 6 | `state` | Agent state tracking (push + pull), delivery decision matrix |
| 7 | `notifications` | Message formatting templates |
| 8 | `delivery` | Delivery orchestration, session intelligence, retry, dedup |
| 9 | `dispatch` | GitHub event routing, issue/comment handling |
| 10 | `monitoring` | Agent health monitoring, stall detection, escalation, heartbeats |
| 11 | `telegram` | Bot with 12 commands, topic routing, workflow integration |
| 12 | `onboarding` | Workspace repo discovery, status checks, automated setup |
| 13 | `streaming` | Control mode, SSE broker, PTY management |
| 14 | `workflows` | Workflow discovery and execution engine |

## Project Structure

```
src/agent_backbone/
  base/              # LifecycleAware protocol, LifecycleManager, exceptions
  services/          # 14 service packages (see table above)
  config.py          # BackboneConfig from TOML + env vars
  settings.py        # Pydantic BaseSettings entry point
  models.py          # Pydantic domain models
api/
  app.py             # FastAPI application factory, lifespan
  routes/            # 18 route modules (webhook, agents, issues, plans, ...)
  deps.py            # FastAPI dependency injection
  auth.py            # API key authentication
gateway/
  server.py          # Legacy standalone gateway
alembic/             # Database migrations
tests/               # 952 tests across 47 files
docs/
  specifications/    # SDD behavioral contracts (31 specs)
  protocols/         # REST API protocol
```

## Development

```bash
make install          # Install dependencies (uv sync)
make test             # Run all tests
make lint             # Ruff check
make format           # Ruff format
make fix              # Auto-fix lint + format
make check            # lint + format-check + tests (CI gate)
make cov              # Tests with coverage

# Database
make db-up            # Start PostgreSQL (Docker, port 5435)
make db-down          # Stop PostgreSQL
make db-upgrade       # Run Alembic migrations
make db-migrate MSG="description"  # Create new migration

# Services
make dev              # Start gateway server
make run-prefect      # Start Prefect server (port 4200)
make setup-pool       # Create agent-pool work pool (one-time)
make deploy           # Deploy all scheduled flows
make run-worker       # Start Prefect worker
```

## Configuration

Two layers: `AppSettings` (Pydantic `BaseSettings`, `BACKBONE_` env prefix) for secrets, then `BackboneConfig.from_toml()` for structural config from `backbone.toml`.

**Required env vars:** `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_APP_WEBHOOK_SECRET`
**Optional:** `TELEGRAM_TOKEN`, `BACKBONE_API_KEY`, `JARVIS_INJECT_URL`, `BACKBONE_DATABASE_HOST/PORT/USER/PASSWORD/NAME`

Tests use in-memory SQLite — no PostgreSQL required for `make test`.

## Docs

- **[USAGE.md](USAGE.md)** — Detailed operational guide
- **[docs/specifications/SPEC_INDEX.md](docs/specifications/SPEC_INDEX.md)** — All behavioral contracts
- **[docs/protocols/REST_API.md](docs/protocols/REST_API.md)** — HTTP endpoint contracts
