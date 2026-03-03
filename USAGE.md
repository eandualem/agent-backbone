# Agent Backbone — Usage Guide

The automation backbone for the multi-agent workspace. Receives GitHub webhook events, routes them to agent tmux sessions, manages delivery lifecycle, and exposes a REST + real-time API for dashboard and mobile control.

```
GitHub webhook --> FastAPI (port 7120) --> Prefect flows --> tmux / Jarvis
                        |                        |
                  14 services on           scheduled flows
                  app.state                via service locator
                        |
                  REST API + SSE + Socket.IO
                        |
                  Telegram bot (mobile)
                        |
                  PostgreSQL (port 5435)
```

---

## Prerequisites

- **Python 3.11+** with **uv** (package manager)
- **Docker** (for PostgreSQL container)
- **tmux** (agent session management)
- **ngrok** (webhook tunnel — free tier works)

---

## First-Time Setup

### 1. Install dependencies

```bash
cd ~/ws/core/code/WF/agent-backbone
make install
```

### 2. Start PostgreSQL

```bash
make db-up
```

This starts PostgreSQL 16 on port **5435** (not 5432 or 5434, to avoid conflicts with other services). Data is persisted in a Docker named volume `agent-backbone_backbone-db`.

### 3. Run database migrations

```bash
make db-upgrade
```

### 4. Environment file

```bash
cp .env.example .env
```

Edit `.env`:

```bash
# Required — GitHub PAT with repo scope
GITHUB_TOKEN=ghp_your_token_here

# Required — webhook signature verification
# Auto-loaded from .webhook-secret (repo root) or ~/.claude/services/.webhook-secret
WEBHOOK_SECRET=your_secret_here

# Optional — Telegram bot
TELEGRAM_TOKEN=your_telegram_bot_token

# Optional — API authentication (dev mode: no key = allow all)
BACKBONE_API_KEY=your_api_key

# Optional — database overrides (defaults match docker-compose.yml)
# BACKBONE_DATABASE_HOST=localhost
# BACKBONE_DATABASE_PORT=5435
# BACKBONE_DATABASE_USER=backbone
# BACKBONE_DATABASE_PASSWORD=backbone
# BACKBONE_DATABASE_NAME=backbone
```

### 5. GitHub webhook config

Same port (7120) and endpoint (`/webhook`) as always. If setting up fresh:

1. Generate secret: `openssl rand -hex 32 > .webhook-secret` (in repo root)
2. Start services: `make start-backbone && make start-tunnel`
3. Configure at https://github.com/eandualem/orchestration/settings/hooks:
   - Payload URL: `https://YOUR-NGROK-URL/webhook`
   - Content type: `application/json`
   - Secret: contents of `.webhook-secret`
   - Events: **Issues** and **Issue comments** only

---

## Starting and Stopping

All commands use `make` targets or the infrastructure module CLI (`uv run python -m agent_backbone.services.infrastructure <command>`).

### Start Everything

```bash
make db-up              # PostgreSQL (if not already running)
make start-backbone     # Prefect server + gateway + worker + telegram
make start-tunnel       # ngrok tunnel
```

### Individual Control

```bash
# Database
make db-up              # Start PostgreSQL container
make db-down            # Stop PostgreSQL container

# Prefect server
uv run python -m agent_backbone.services.infrastructure start-prefect
uv run python -m agent_backbone.services.infrastructure stop-prefect

# Gateway
make dev                # Start/restart gateway with auto-reload
uv run python -m agent_backbone.services.infrastructure stop-gateway

# All backbone services
make start-backbone
make stop-backbone
```

### Check What's Running

```bash
make infra-status
```

---

## Database Management

### Daily Usage

PostgreSQL runs as a Docker container. Start it once and leave it running:

```bash
make db-up            # Start (idempotent — safe to run multiple times)
make db-down          # Stop when done
```

### Migrations

```bash
make db-upgrade                     # Run pending migrations
make db-migrate MSG="add new table" # Create a new migration
```

### Connection Details

| Setting | Value |
|---------|-------|
| Host | `localhost` |
| Port | `5435` |
| User | `backbone` |
| Password | `backbone` |
| Database | `backbone` |
| Async URL | `postgresql+asyncpg://backbone:backbone@localhost:5435/backbone` |
| Sync URL | `postgresql://backbone:backbone@localhost:5435/backbone` |

### Tests Use In-Memory SQLite

Tests do **not** require PostgreSQL. They use `sqlite+aiosqlite:///:memory:` for fast isolation. All SQL is written in standard SQL compatible with both dialects.

---

## How It Works

### Issue Dispatch (new issues + comments)

```
GitHub webhook (issue opened/labeled/commented)
  --> Gateway validates signature, deduplicates
  --> Dispatch service routes to targets
  --> Delivery service checks agent state
  --> Delivers notification to tmux session (or Jarvis)
```

### Close-Then-Next

When an agent closes an issue:
1. Dispatch service identifies the `for:` target entities
2. Queries GitHub for remaining open issues with that `for:` label
3. Sorts by priority score (blocking first, then type weight, then age)
4. Dedup check — skip if same issue was recently notified
5. Delivers the highest-priority next issue

### Agent Monitor (offline recovery)

Every 60 seconds:
- Check all entity tmux sessions
- For each online, idle agent: query GitHub for open `for:{entity}` issues
- Deliver pending issues to newly-online agents
- Detect stalled agents (90+ min processing) and escalate to Ike
- Push Telegram notifications for agents awaiting plan approval

### Delivery Retry

Every 5 minutes:
- Query database for failed/offline/deferred deliveries
- Re-attempt via `safe_deliver()` with state checks
- Drain the message queue (deferred messages for offline agents)

---

## Telegram Bot

Mobile control with 12 commands:

| Command | Purpose |
|---------|---------|
| `/status` | Active tmux sessions |
| `/queue` | Failed/pending deliveries |
| `/digest` | Full system digest |
| `/tell <agent> <msg>` | Deliver message via `safe_deliver()` |
| `/start <agent>` | Start agent session |
| `/stop <agent>` | Stop agent session |
| `/workflow [name]` | List or execute workflows |
| `/viewplan <agent>` | Read plan file content |
| `/approve <agent>` | Approve plan (Shift+Tab) |
| `/identify` | Report topic thread ID |
| `/help` | Command list |

**Topic routing:** Forum threads map to tmux sessions via config + auto-discovery.

---

## REST API

All routes authenticated via `BACKBONE_API_KEY` Bearer token. Webhook uses HMAC signature verification.

Key endpoints:

| Endpoint | What It Does |
|----------|-------------|
| `/health` | Aggregated service health (unauthenticated) |
| `/api/agents` | Agent list with enriched state |
| `/api/agents/{session}/start` | Start session (claude, gemini, codex, shell) |
| `/api/agents/{session}/stop` | Stop session |
| `/api/agents/{session}/message` | Send message to agent |
| `/api/agents/{session}/stream` | SSE stream — real-time tmux output |
| `/api/issues` | Issue list with priority scores |
| `/api/plans` | Plan review and approval |
| `/api/deliveries` | Delivery history |
| `/api/workflows` | Workflow listing and execution |
| `/api/rooms` | Room lifecycle and messaging |
| `/api/repos` | Repository discovery and onboarding |
| `/webhook` | GitHub webhook intake |

Full protocol: [docs/protocols/REST_API.md](docs/protocols/REST_API.md)

---

## Architecture

### Service Layer

14 services under `src/agent_backbone/services/`, each following the sub-module pattern:
- `interface.py` — thin LifecycleAware class (start/stop/health_check)
- `factory.py` — registration with LifecycleManager
- `exceptions.py` — service-specific errors
- `_*.py` — private implementation modules

### File Layout

```
~/ws/core/code/WF/agent-backbone/
├── src/agent_backbone/
│   ├── base/                  # LifecycleAware protocol, LifecycleManager
│   ├── services/
│   │   ├── database/          # Engine lifecycle, ORM models, session factory
│   │   ├── persistence/       # Query repos (delivery, state, queue, dedup)
│   │   ├── registry/          # Entity/repo registry
│   │   ├── github/            # GitHub REST API client
│   │   ├── tmux/              # Async tmux operations
│   │   ├── state/             # Agent state push+pull
│   │   ├── notifications/     # Message formatting
│   │   ├── delivery/          # Delivery orchestration, session intelligence
│   │   ├── dispatch/          # Event routing, comment handling
│   │   ├── monitoring/        # Health monitoring, escalation, heartbeats
│   │   ├── telegram/          # Bot commands, topic routing
│   │   ├── onboarding/        # Repo discovery, setup pipeline
│   │   ├── streaming/         # Control mode, SSE broker, PTY
│   │   └── workflows/         # Workflow engine and registry
│   ├── config.py              # BackboneConfig (TOML + env)
│   ├── settings.py            # Pydantic BaseSettings entry point
│   └── models.py              # Pydantic domain models
├── api/
│   ├── app.py                 # FastAPI app factory, lifespan
│   ├── routes/                # 18 route modules
│   ├── deps.py                # FastAPI dependency injection
│   └── auth.py                # API key auth
├── gateway/server.py          # Legacy standalone gateway
├── alembic/                   # Database migrations
├── tests/                     # 952 tests, 47 files
├── docker-compose.yml         # PostgreSQL 16 on port 5435
├── backbone.toml              # Structural configuration
├── .env                       # Secrets (not committed)
└── Makefile                   # All development commands
```

---

## Troubleshooting

### PostgreSQL won't start

```bash
# Check if port 5435 is in use
lsof -i :5435

# Check Docker container status
docker compose ps

# View container logs
docker compose logs backbone-db

# Restart
make db-down && make db-up
```

### Port conflict with lovely-assistant

The backbone uses port **5435** specifically to avoid conflict with lovely-assistant on **5434**. If you see connection errors, verify the port in `docker-compose.yml` and `backbone.toml`.

### Migration errors

```bash
# Check current migration state
uv run alembic current

# Reset and re-run
uv run alembic downgrade base
make db-upgrade
```

### Gateway won't start — port in use

```bash
lsof -i :7120
kill $(lsof -ti :7120)
make dev
```

### Notifications not arriving

Check in order:
1. **Services running?** `make infra-status`
2. **Database up?** `make db-up` then check `/health` endpoint
3. **Gateway receiving?** `tmux attach -t gateway`
4. **GitHub delivering?** Check webhook recent deliveries in GitHub settings
5. **Target agent running?** `tmux ls`
6. **Flow failed?** Check Prefect dashboard at http://localhost:4200

### Tests failing

```bash
# Tests use in-memory SQLite — no Docker needed
make test

# Single test file
make test-file FILE=tests/test_persistence.py
```

---

## Quick Reference

```bash
# Setup
make install && make db-up && make db-upgrade

# Start everything
make start-backbone && make start-tunnel

# Stop everything
make stop-backbone && make stop-tunnel && make db-down

# Check status
make infra-status

# Run tests
make check

# Prefect dashboard
open http://localhost:4200
```
