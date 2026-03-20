# Library Replacement Audit — agent-backbone

**Issue:** [eandualem/agent-backbone#39](https://github.com/eandualem/agent-backbone/issues/39)
**Date:** 2026-03-20
**Branch:** `swarm/39/audit`

---

## Scope and Method

**Repository:** `eandualem/agent-backbone` — an AI agent orchestration backbone that manages coding agents via tmux sessions, routes GitHub issue notifications, ingests CLI telemetry, and coordinates swarm workflows.

**Codebase size:** ~26,000 lines of Python across 130+ files in 11 service packages (`agents`, `analytics`, `automation`, `database`, `github`, `infrastructure`, `registry`, `routing`, `telegram`, `telemetry`, `terminal`) plus API routes and configuration.

**Audit method:** 4 scouts working in parallel (2 Claude Opus 4.6, 2 GPT-5.4), each independently auditing the entire repository with sub-agents for code exploration and web research. A dedicated validator (GPT-5.4) reconciled the four reports, resolving 9 disagreements with evidence-based verdicts.

**Audit question:** For every module, utility, or subsystem, is there a well-known, maintained library that already handles the same functionality? Focus on substantial implementations, not trivial helpers.

---

## Findings

### Finding 1: Raw SQL in Repository Modules — Migrate to SQLAlchemy Core

| Dimension | Detail |
|---|---|
| **What we built** | `services/database/_swarm_repo.py` (897 LOC) and `services/database/_queue_repo.py` (535 LOC) use raw `sqlalchemy.text()` for every query. This includes hand-built parameterized IN-lists (`:id_0, :id_1, ...`), f-string SQL for dynamic WHERE clauses, and dialect-specific branching for `FOR UPDATE SKIP LOCKED` and `ON CONFLICT`. |
| **What exists** | **SQLAlchemy Core query builders** (`select()`, `insert()`, `update()`, `delete()`) — already a dependency (SQLAlchemy >=2.0). The ORM models in `models.py` already use modern `Mapped[T]`/`mapped_column` declarative style. This is better use of an existing dependency, not a new library. |
| **Fit assessment** | **Exact fit.** `col.in_(values)` replaces manual IN-list parameterization. `insert().on_conflict_do_nothing()` replaces raw `ON CONFLICT` SQL. `with_for_update(skip_locked=True)` handles dialect branching natively. |
| **Migration risk** | **Medium.** Requires careful testing across SQLite (tests) and PostgreSQL (production). Some cases need raw SQL: RETURNING with COALESCE, partial indexes, complex upserts. Estimated reduction: 100–200 lines of boilerplate. |
| **Recommendation** | **Migrate incrementally.** Start with simple CRUD in `_queue_repo.py`, then tackle `_swarm_repo.py`. Priority: medium — do it when touching these files for other reasons. Eliminates manual parameterization that is a latent injection risk. |
| **Scout consensus** | 3/4 scouts flagged this (both Claude scouts + validator uplift). Codex scouts focused on external libraries and missed this internal-improvement opportunity. |

**Sources:**
- [SQLAlchemy 2.0 Core Tutorial](https://docs.sqlalchemy.org/en/20/tutorial/data.html)
- [SQLAlchemy Async I/O](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)

---

### Finding 2: GitHub App Auth — Investigate `githubkit` for Partial Replacement

| Dimension | Detail |
|---|---|
| **What we built** | `services/github/interface.py` (427 LOC) — async httpx-based GitHub App client with hand-rolled RS256 JWT construction using `cryptography`, installation token caching with 1-minute refresh skew, and 9 domain-specific API methods (list/get/create/update issues, comments, sub-issues). |
| **What exists** | **githubkit** (325 GitHub stars, ~1.25M monthly PyPI downloads, v0.15.0 March 2026) — async-first, httpx-native, typed GitHub SDK with built-in `GitHubAppInstallationAuthStrategy`. Also evaluated: **gidgethub** (405 stars, async sans-I/O, auth helpers) and **PyGithub** (7.7K stars, sync-only — hard disqualifier for this async stack). |
| **Fit assessment** | **Partial fit (auth: high, API methods: low).** githubkit's auth strategy could replace ~100 LOC of JWT + token cache plumbing. The 9 domain-specific API methods (with `ParsedLabels`, `from:`/`for:` label semantics) remain custom regardless. |
| **Migration risk** | **Medium.** githubkit is 0.x with a single maintainer — bus-factor risk. Per-repo token caching/refresh skew logic may still need custom handling. Existing `respx` HTTP mocking in tests needs adjustment. |
| **Recommendation** | **Investigate further, do not adopt today.** Monitor githubkit for 1.0 stability. When it matures, the auth layer replacement becomes favorable (~100 LOC savings). The domain API wrappers stay custom either way. |
| **Scout consensus** | 4/4 scouts identified this area. Validator resolved the gidgethub vs githubkit disagreement in favor of githubkit (better async/httpx fit for this stack). All agreed on investigate-only, not immediate replacement. |

**Sources:**
- [githubkit GitHub](https://github.com/yanyongyu/githubkit), [docs](https://yanyongyu.github.io/githubkit/)
- [gidgethub GitHub](https://github.com/gidgethub/gidgethub), [apps module](https://gidgethub.readthedocs.io/en/stable/apps.html)
- [PyGithub async issue #1538](https://github.com/PyGithub/PyGithub/issues/1538)

---

## Areas Reviewed — Kept Custom

### Terminal / tmux Management (~2,300 LOC)

**Files:** `services/terminal/_core.py`, `_sessions.py`, `_panes.py`, `_windows.py`, `_adapters.py`, `_pty.py`, `_streaming.py`, `_copy_mode.py`

Custom async tmux wrapper with concurrency semaphore (max 5), per-call timeouts, `load-buffer`/`paste-buffer` delivery, 7 CLI runtime adapters (prompt detection, ANSI parsing), PTY management with incremental UTF-8 decoding, and Socket.IO terminal bridge.

**Library evaluated:** **libtmux** (1.2K stars, v0.55.0) — sync-only, no `load-buffer`/`paste-buffer` in public API, no async support. Codex scouts recommended investigation; Claude scouts and validator correctly identified the sync-only and missing-operations blockers.

**Verdict:** Keep custom. Revisit only if libtmux ships native asyncio support.

---

### Message Routing & Delivery (~2,900 LOC)

**Files:** `services/routing/_delivery.py`, `_router.py`, `_flows.py`, `_lifecycle.py`, `_dedup.py`, `database/_queue_repo.py`

9-state session intelligence, entity-to-session resolution, state-aware delivery (enqueue if busy, deliver if idle), content-hash dedup, priority scoring, close-then-next workflow, dependency tracking, retry with backpressure.

**Libraries evaluated:** Celery, dramatiq, arq, huey, procrastinate — all assume the consumer is a Python callable. The backbone's "consumer" is a tmux pane. Fundamental model mismatch.

**Verdict:** Keep custom. The queue repo's raw SQL is addressed in Finding 1.

---

### Telemetry Collection (~1,750 LOC)

**Files:** `services/telemetry/_adapters.py` (1,441 LOC), `_collector.py`

5 CLI runtime adapters parsing proprietary transcript formats (Claude JSONL, Codex JSONL, Gemini JSON, OpenCode SQLite, Cursor). Incremental checkpoint-based collection with token bucket unification.

**Libraries evaluated:** OpenTelemetry, prometheus-client, structlog, Langfuse, Arize Phoenix — all are *emission* frameworks. This is *ingestion* of external CLI artifacts. Completely orthogonal domains.

**Verdict:** Keep custom. No library knows how to parse Claude Code JSONL transcripts.

---

### Agent Monitoring & Escalation (~2,200 LOC)

**Files:** `services/agents/_monitor.py`, `_heartbeat.py`, `_escalation.py`, `_state.py`, `_pending.py`

Custom agent state machine (7 states), push/pull state reconciliation, heartbeat scheduling, stall/offline/plan-waiting detection with dedup, copy-mode auto-recovery.

**Verdict:** Keep custom. No library exists for AI agent state management with tmux integration.

---

### Automation & Onboarding (~3,000 LOC)

**Files:** `services/automation/_pipeline.py`, `_flows.py`, `_engine.py`, `_registry.py`

10-step onboarding pipeline, JSON workflow engine, Prefect flow definitions. Prefect is already correctly used for orchestration.

**Verdict:** Keep current approach. Domain logic has no library replacement.

---

### Configuration & Lifecycle (~700 LOC)

**Files:** `config.py` (433 LOC), `settings.py` (83 LOC), `base/lifecycle.py` (92 LOC), `services/_locator.py` (111 LOC)

TOML config with frozen dataclasses, env var overlay via pydantic-settings, ordered lifecycle startup/shutdown, service locator for Prefect subprocesses.

**Libraries evaluated:** dependency-injector (async teardown bugs), dishka (no ordered startup), pydantic-settings TOML migration (high effort, marginal gain).

**Verdict:** Keep custom. Already uses pydantic-settings in the right place. LifecycleManager is 92 lines — no library does this better.

---

### Telegram, SocketIO, Analytics, Infrastructure, Registry

| Area | LOC | Status |
|------|-----|--------|
| Telegram bot | ~700 | Already uses python-telegram-bot correctly |
| SocketIO terminal bridge | 623 | Already uses python-socketio correctly |
| Analytics aggregation | 835 | Domain-specific normalization; no library fit |
| Infrastructure / process mgmt | 574 | Domain-specific tmux lifecycle; supervisor can't manage it |
| Entity registry | ~460 | Domain-specific Lovely Universe topology |

---

## Maintenance Items (Non-Audit)

These are not reinvention findings but surfaced during the audit:

1. **croniter dependency pin:** Project declares `croniter>=3.0`. The package transferred to `pallets-eco` in late 2024; update pin to `>=6.0` to track the maintained release line.

2. **psutil for process inspection:** `services/infrastructure/_processes.py` (175 LOC) uses `lsof` and PID file management. `psutil` (~11K GitHub stars) could replace some of this, but the module is small and the improvement is marginal. Defer unless portability becomes an issue.

---

## Ranked Recommendation Summary

| Rank | Area | Action | Priority | Estimated Impact |
|------|------|--------|----------|-----------------|
| 1 | Raw SQL in `_queue_repo.py` + `_swarm_repo.py` | **Migrate to SQLAlchemy Core** | Medium | -100–200 LOC, safer parameterization |
| 2 | GitHub App auth in `interface.py` | **Investigate githubkit** (when 1.0 ships) | Low | -100 LOC auth plumbing |
| 3 | croniter dependency pin | **Update to >=6.0** | Low | Dependency hygiene |
| 4 | psutil for `_processes.py` | **Defer** | Low | Optional cleanup |

**Libraries already correctly used:** FastAPI, SQLAlchemy (ORM layer), Prefect, python-telegram-bot, python-socketio, pydantic-settings, croniter, httpx, Alembic, cryptography.

**Key conclusion:** The codebase has minimal library-replaceable reinvention. The vast majority of custom code exists because the domain — delivering messages to AI agents in tmux sessions, ingesting telemetry from 5 CLI runtimes, managing multi-state agent lifecycles — has no library equivalent.

---

## Source Reports

| Scout | CLI | Report | Size |
|-------|-----|--------|------|
| swarm-39-scout-claude-1 | Claude Opus 4.6 | `.swarm/scout-swarm-39-scout-claude-1.md` | 18.7K |
| swarm-39-scout-claude-2 | Claude Opus 4.6 | `.swarm/scout-swarm-39-scout-claude-2.md` | 21.0K |
| swarm-39-scout-codex-1 | GPT-5.4 | `.swarm/scout-swarm-39-scout-codex-1.md` | 4.2K |
| swarm-39-scout-codex-2 | GPT-5.4 | `.swarm/scout-swarm-39-scout-codex-2.md` | 7.1K |
| swarm-39-validator | GPT-5.4 | `.swarm/validator-report.md` | 12.3K |
