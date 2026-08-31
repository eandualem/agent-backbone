# agent-backbone — Architecture Proposal (v2 reboot)

Status: **AGREED** — 2026-08-31 (decisions in §7 recorded; Phase 1 executed on branch `v2`)
Scope: what the open-source backbone *is*, what it is *not*, and the shape it takes before any cleanup starts.

---

## 1. Assessment of the current codebase

Numbers: ~26K lines in `src/`, ~31K in `tests/` (1,489 tests, 2 currently failing), 128 locked dependencies, 66 commits between 2026-02-11 and 2026-03-18. README/USAGE describe a layout that no longer exists (`gateway/server.py`, `docs/specifications`, "14 services" — there are 11, with different names).

### 1.1 The kernel — what is genuinely valuable and worth keeping

| Piece | Where | Why it matters |
|---|---|---|
| State-gated delivery (`safe_deliver`) | `services/routing/_delivery.py` | The real IP. A message is only pasted into an agent when a composite readiness check passes; otherwise it is durably queued, retried, deduplicated, and claimed atomically. Semantics are right, implementation is verbose. |
| Session intelligence derivation | `services/routing/_intelligence.py` | The priority chain (offline → plan_waiting → permission_waiting → working → copy_mode → user_interacting → idle) is a sound readiness model. |
| Push + pull state reconciliation | `services/agents/_inference.py` | Hooks push state; terminal pull verifies. Correct idea — but the hooks that do the pushing are **not in the repo** (they lived in `~/.claude`). |
| Per-CLI terminal adapters | `services/terminal/_adapters.py` | Claude / Codex / Gemini / OpenCode / Aider / Cursor / shell with paste-then-submit strategies. Right abstraction; the runtime *detection* by screen-scraping (`"opus 4.6"`, `"gpt-5."`, `"gemini 3"`) is brittle and will silently break. |
| GitHub Issues as the shared task ledger | `models.py`, `routing/_router.py`, `routing/_lifecycle.py` | `for:` / `from:` / type / `blocking` labels; close-then-next. Durable, human-visible, inspectable, works from any client. Good. |
| Durable queue + dedup + claims | `services/database/_queue_repo.py`, `_delivery_repo.py` | Partial unique indexes, leases, content-hash dedup. Solid. |
| Telegram control surface | `services/telegram/` | `/tell`, `/status`, topic ↔ agent routing, auto-discovery. Good mobile story. |
| Engineering hygiene | `base/lifecycle.py`, Alembic, SQLite-in-tests | Keep. |

### 1.2 What makes it unusable by anyone else

1. **A second, undocumented half lives outside the repo.** Agent state files (`~/.claude/state/{session}.json`), the action log (`~/.claude/state/github-actions.jsonl`), heartbeat schedules, Telegram topic cache, and the Claude Code hooks that write them. Without those, readiness detection degrades to screen-scraping.
2. **Filesystem convention as configuration.** `~/ws/core/code/{Org}/{repo}` is *the* agent discovery mechanism; `entity-registry.json` adds roles/instances/organizations/groups on top. This is the "must structure your files this way" problem.
3. **The author's environment is hardcoded in ~25 source files.** `eandualem/orchestration`, skip `elias`, fallback/escalation `ike`, `Africa/Addis_Ababa`, the `jarvis` HTTP special-case threaded through delivery + resolution, the `coding-agent` magic target, a `hierarchy` endpoint with Jarvis / Da Vinci / Feynman / Brunel / Eisenhower / Bell(WF) / Bell(Loveble) as constants, `make start-arclio|loveble|wf`, `~/notes`, `ROUTINE.md`, an absolute path in `prefect.yaml`, a personal GitHub App ID in `.env.example`.
4. **Heavy operational floor.** Docker + PostgreSQL + Prefect server + Prefect worker + work pool + deployments + ngrok + a GitHub App are required to run five periodic jobs (monitor/60s, retry/300s, heartbeat/60s, morning, evening). Prefect alone explains `_locator.py`, the supervisor code in `infrastructure/_backbone.py`, and a separate SQLite database.
5. **Scope creep from the Lovely Universe era** (roughly 40 % of `src/`): swarms (4 tables, 553-line route), rooms, hierarchy, notes, schedule/ROUTINE.md, files browse/write, analytics, telemetry adapters (1,441 lines parsing the private log formats of five CLIs), onboarding pipeline, morning/evening routines, Socket.IO PTY terminals, dashboard aggregate.
6. **Security posture is "trusted laptop".** API key optional (default: allow all, with a warning); CORS `*` with credentials; webhook signature only checked *if* a secret is configured; an endpoint that reads/writes files; commenter identity taken from a spoofable `[from:X]` text tag; plan approval = injecting keystrokes from a chat command.
7. **Single-repo data model.** Deliveries, acks, queue, dependencies are keyed by bare `issue_number`. Two repos with issue #12 collide.

### 1.3 Relationship to Odaa / Lovely Universe

The Odaa dossier describes an *epistemic governance* layer (Actors, Perspectives, Frontiers, cuts, templates, settlement). Its own conclusion applies here: *"execution orchestration is increasingly commodity … the substrate boundary needs an explicit architectural home."* The backbone **is that substrate**, and it should stay ontology-free. Three Odaa lessons do carry over as engineering constraints, not concepts:

- every delivered action leaves a reviewable trace (→ delivery log + issue comments);
- constrain the boundary, not the reasoning (→ one typed `Message` envelope, nothing else crosses into an agent);
- don't let a metaphor dictate topology (→ no hierarchy/org/role model in the core; agents are a flat set with optional tags).

---

## 2. Positioning

> **agent-backbone** is a local control plane for terminal AI agents (Claude Code, Codex, Gemini CLI, OpenCode, Aider, …). It starts and stops them, delivers messages to them safely, lets them talk to each other and to you, and coordinates multi-agent work through GitHub Issues — from your laptop, from Telegram, or from any HTTP client.

It is **not** an agent framework, a workflow engine, a dashboard, or an organizational model. Anything of that kind sits *on top of* the API.

Design values, in priority order: **plug-and-play → safe by default → small → extensible**.

---

## 3. Core concepts (the whole vocabulary)

| Concept | Definition | Replaces |
|---|---|---|
| **Agent** | A named, configured terminal agent: `name`, `dir`, `runtime` (claude/codex/…), optional `model`, `env`, `tags`. Declared in config; no directory convention. | entity registry, roles, instances, organizations, groups, repo discovery |
| **Session** | A running instance of an Agent inside a session backend (tmux today). Has a readiness state. | tmux session + state file + pane scraping |
| **Runtime** | A pluggable adapter for one CLI: launch command, prompt/idle detection, submit strategy, hook installer. | `TerminalAdapter` (kept, promoted to plugin) |
| **Message** | The *only* thing that enters an agent: `to`, `from`, `via`, `kind`, `body`, `ref` (optional task ref), `priority`. Every channel produces Messages; one delivery engine consumes them. | ad-hoc strings with `[via:… from:…]` prefixes |
| **Delivery** | One attempt to hand a Message to a Session, with outcome, queueing, retry, dedup. | `safe_deliver` (kept, restructured) |
| **Channel** | A source and/or sink of Messages: GitHub Issues, Telegram, HTTP API, CLI, agent-to-agent. | webhook route, telegram service, `/api/messages`, jarvis |
| **Task** | A unit of work in a tracker, addressed as `(repo, number)`. GitHub Issues is the default and only tracker in v2, behind a `TaskTracker` interface. | issue_number-keyed everything |
| **Schedule** | A cron/interval that emits a Message (e.g. `08:00 → tell reviewer "daily triage"`). | heartbeats, morning/evening routines |

Readiness states stay as they are: `offline · starting · idle · busy · plan_waiting · permission_waiting · user_interacting · copy_mode · unknown`.

---

## 4. Architecture

```
┌──────────── channels ─────────────┐        ┌──────── runtimes (plugins) ────────┐
│ github   telegram   http   cli    │        │ claude  codex  gemini  opencode …  │
└──────┬──────┬────────┬──────┬─────┘        └──────────────┬─────────────────────┘
       │      │        │      │        Message                │ launch / detect / submit / hooks
       ▼      ▼        ▼      ▼                               ▼
┌────────────────────── core ──────────────────────┐   ┌─── sessions ───┐
│ DeliveryEngine  Readiness  Queue  Dedup  Retry   │◄──┤ tmux backend   │
│ TaskTracker(iface)  Scheduler(asyncio)  Events   │   └────────────────┘
└──────────────────────┬───────────────────────────┘
                       ▼
             ┌──── store ────┐        ┌──── api ────┐
             │ SQLite (default)│      │ FastAPI, SSE │ ◄── dashboards, agents, scripts
             │ Postgres (opt) │        └─────────────┘
             └────────────────┘
```

**One process.** `backbone up` runs API + scheduler + Telegram bot + GitHub connector in a single asyncio loop (optionally supervised in tmux). No Prefect, no worker, no work pool, no service locator.

### 4.1 Package layout (target)

```
agent_backbone/
  core/         models.py (Agent, Message, Delivery, Task), delivery.py, readiness.py,
                queue.py, scheduler.py, events.py, tracker.py (TaskTracker protocol)
  runtimes/     base.py (Runtime protocol + registry via entry points),
                claude.py, codex.py, gemini.py, opencode.py, aider.py, shell.py
  sessions/     base.py (SessionBackend protocol), tmux.py
  channels/     github/ (webhook + polling, issue formatter, label conventions),
                telegram/, http/ (routes), cli/
  store/        sqlalchemy models, repos, alembic
  hooks/        shipped hook scripts per runtime (state push, action log) — installed by CLI
  config.py     one TOML schema (pydantic-settings), env overrides
  cli.py        backbone init | up | down | status | doctor | agent … | tell | hooks install
  app.py        assembles everything
```

### 4.2 Readiness: hooks first, scraping second

- The package **ships its own hooks** (`backbone hooks install claude` writes them into the agent's settings). Hooks `POST /api/agents/{name}/state` (idle/busy/plan_waiting/permission_waiting, current task ref) and `POST /api/agents/{name}/actions` (e.g. "commented on repo#12"). No files under `~/.claude/state`.
- Pane scraping remains as the **fallback** for runtimes without hooks, and as a liveness cross-check. Runtime identity comes from the launch (`BACKBONE_RUNTIME` env, already present) — never from guessing model names in the pane.
- Because agents post GitHub comments **through the backbone** (`backbone task comment`, or the hook records it), origin is known and the spoofable `[from:X]` tag goes away.

### 4.3 Configuration (plug-and-play contract)

One file, `backbone.toml`, found by walking up from CWD or at `~/.config/agent-backbone/backbone.toml`. Secrets via env / `.env`.

```toml
[backbone]
data_dir = "~/.local/share/agent-backbone"   # SQLite lives here by default
bind = "127.0.0.1:7120"

[agents.reviewer]
dir = "~/code/my-app"
runtime = "claude"
model = "claude-opus-5"

[agents.builder]
dir = "~/code/my-app"
runtime = "codex"

[tracker]                       # optional
kind = "github"
repo = "me/my-app"              # default repo for `backbone task new`
mode = "poll"                   # or "webhook" (then a public URL + secret are needed)

[telegram]                      # optional
allowed_chat_ids = [123456789]
```

`backbone init` generates this interactively, generates an API key, and runs `doctor` (tmux present? runtimes on PATH? gh auth?). No Docker, no Postgres, no tunnel, no GitHub App required — PAT / `gh auth token` works; App and webhook are the "advanced" path.

### 4.4 API (small, stable)

`/api/agents` (list, start, stop, state) · `/api/messages` (send) · `/api/deliveries` (history, queue) · `/api/tasks` (proxy to tracker, create/comment/close with provenance) · `/api/schedules` · `/api/events` (SSE) · `/health`. That is the whole public surface. Terminal streaming (`/api/agents/{n}/stream`) is kept as *optional* (feature flag) since it is small and useful from a phone.

### 4.5 Security defaults (open-source bar)

- API key **required**; generated at `init`; `--no-auth` only with an explicit flag and a loud warning.
- Bind `127.0.0.1`; CORS off unless origins are configured; no wildcard-with-credentials.
- Webhook mode refuses to start without a secret; polling mode needs none.
- Telegram refuses to start with an empty allowlist.
- No filesystem browse/read/write endpoints. No notes. No arbitrary keystroke endpoint; plan approval is a runtime-adapter capability with an explicit `[security] allow_remote_approve = true` opt-in.
- Everything that reaches an agent is wrapped in one envelope with provenance (`via`, `from`, `ref`) and documented as **untrusted input** — the backbone cannot prevent prompt injection in a GitHub comment, but it must never launder it as trusted.
- Secrets never in `.env.example`; `doctor` warns on world-readable keys.

---

## 5. Cut list

| Remove | Lines (approx.) | Reason |
|---|---|---|
| Prefect (server, worker, flows, `_locator`, supervisors, `prefect.yaml`) | ~1,500 + dep tree | Five timers do not justify a workflow platform. Replaced by an in-process asyncio scheduler. |
| swarms, rooms, hierarchy, notes, schedule, files, analytics, dashboard, actions, activity, prefect routes + tables + tests | ~5,000 | Dashboard/Lovely-Universe features; not backbone. |
| telemetry adapters (5 CLI log parsers) + analytics service | ~2,600 | Brittle, private formats; out of scope. Could return as an optional plugin. |
| automation: onboarding pipeline, workflows engine, morning/evening routines | ~1,600 | Replaced by `Schedule → Message` and `backbone init`. |
| entity registry (roles, instances, organizations, groups, repo discovery) | ~500 | Replaced by `[agents.*]` in config. |
| jarvis HTTP special-case, `coding-agent` magic target, title-based repo extraction | scattered | Generalize to an `http` runtime/sink or drop. |
| Socket.IO PTY terminals | ~1,000 | Keep SSE read-only stream; drop interactive PTY (security + size). |
| Docker Compose / Postgres as default | — | Postgres stays supported, opt-in. |

Estimated result: **~9–10K lines of `src/`** with the same kernel behaviour, ~30 direct dependencies fewer.

---

## 6. Phases

0. **Agree** on this document. Pick name & license. Fix README so it describes reality.
1. **Cut.** Delete the scope-creep modules and their tests; replace Prefect with the asyncio scheduler; remove every hardcoded name; make the suite green. (Largest diff, mostly deletions.)
2. **Plug-and-play.** `backbone.toml` + `init` + `doctor`; SQLite default; PAT auth; polling mode; hooks shipped and installed by the CLI; `(repo, number)` task keys.
3. **Harden & refine.** Security defaults from §4.5; restructure `safe_deliver` into a `DeliveryEngine` with a decision table; runtime plugin registry; readiness tests per runtime with recorded pane fixtures.
4. **Release.** Docs with a 5-minute quickstart, examples (single agent / two agents + issues / Telegram), CI, PyPI.

---

## 7. Decisions (recorded 2026-08-31)

1. **Prefect dropped.** Replaced by `services/scheduler.py`, an in-process asyncio interval scheduler (monitor, delivery retry, prune).
2. **Dashboard surface dropped; Socket.IO kept.** The backbone emits `sessions:update` snapshots on `/sessions` and streams terminals read-only on `/terminal`. Hierarchy, rooms, swarms (old model), notes, schedule, files, analytics, telemetry adapters, onboarding, morning/evening routines are gone.
3. **Swarms deferred, to be redesigned** as a thin composition over agents + issues (`backbone swarm create --repo X --task X#12 --workers 3`): worktrees + N sessions tagged with a swarm id + a brief. Phase 3.
4. **GitHub Issues** is the only tracker in v2, addressed through the `GitHubClient`; multi-repo via `AgentSpec.repo`. A polling mode is configured (`[github] mode = "poll"`) but not yet implemented — webhook mode works today; `gh webhook forward` is the recommended no-tunnel path.
5. **Name kept** (`agent-backbone`); **MIT** license added (change it if you prefer Apache-2.0 before publishing).
6. **Carved in place** on branch `v2`, keeping the kernel and its tests.

### Phase 1 outcome

- `src/` went from ~26K to ~11K lines; 128 → ~60 locked dependencies.
- Configuration is a single `backbone.toml` with `[agents.<name>]` tables; no directory conventions, no `~/.claude` paths, no hardcoded names.
- SQLite is the default database; one squashed Alembic migration.
- `backbone` CLI: `init`, `doctor`, `up [--detach|--reload]`, `down`, `status`, `agent list|start|stop|start-all|stop-all`, `tell`.
- Security defaults from §4.5 are implemented (API key required, webhook secret required, Telegram allowlist required, remote plan control opt-in, read-only terminal streaming).

### Still ahead

- Phase 2: shipped hooks (`backbone hooks install claude`) that POST state to the API; GitHub polling mode; `Schedule → Message` cron feature.
- Phase 3: restructure `safe_deliver` into a decision table; runtime plugin registry; swarm redesign; recorded-pane fixtures per runtime.
- Phase 4: docs site, examples, CI, PyPI release.
