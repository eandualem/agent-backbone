# Configuration

There is no configuration file. The **data directory** is the configuration:

```
~/.local/share/agent-backbone/      # $BACKBONE_DATA_DIR to move it
├── .env            secrets only (API key, tokens) — never in the database
├── backbone.db     settings, agents, watches, events, deliveries, queue, state
├── state/          hook-written agent state, actions.jsonl, plans/
└── hooks/          the installed hook script
```

- **Settings** are keys with built-in defaults, stored in the database and
  edited with `backbone config set KEY VALUE` (or `PUT /api/config/{key}`).
  The running backbone applies a change immediately.
- **Agents** are discovered by `backbone agent start` and edited with
  `backbone agent set|watch|unwatch|forget`.
- **Secrets** come from `<data_dir>/.env` (loaded at startup) or the
  environment. Only that one file is read — a `.env` in the current
  working directory is ignored.

Only two knobs live outside the directory: `BACKBONE_DATA_DIR` (where it
is) and `BACKBONE_DATABASE_URL` (PostgreSQL instead of the SQLite file).

## Settings

`backbone config list` prints every key with its current value and marks
the ones you changed. Values are JSON (`7999`, `true`, `'["a","b"]'`,
`'{"42":"reviewer"}'`); plain strings need no quoting.

### `backbone.*`

| Key | Default | Meaning |
|---|---|---|
| `backbone.host` | `127.0.0.1` | Bind address. Keep it local unless you put auth and TLS in front |
| `backbone.port` | `7120` | API port |
| `backbone.session_name` | `backbone` | tmux session used by `backbone up --detach` |
| `backbone.cors_origins` | `[]` | Browser origins allowed to call the API; empty disables CORS |
| `backbone.max_delivery_ids` | `100` | In-memory webhook/poll dedup cache (the events table keeps the full log) |

### `agents.*`

| Key | Default | Meaning |
|---|---|---|
| `agents.default_runtime` | `claude` | Runtime used by `agent start` when none is given |
| `agents.pre_trust` | `true` | Answer the runtime's folder-trust dialog before starting, so it never blocks an unattended start: Claude Code and Codex get the same trust record their own dialog writes; Gemini is launched with `--skip-trust`. Starting an agent in a directory is treated as the trust decision; set `false` to answer the dialog yourself |
| `agents.inject_brief` | `true` | Give each agent the backbone's common brief at launch — who it is, how to message other agents, and where to get details (`backbone help`). Claude Code appends it to the system prompt (complementing the project's CLAUDE.md); Codex, Gemini and OpenCode receive it as the session's initial prompt (not re-sent on `--resume`); `aider` and `cursor` receive it as their first delivered message; plain shells get none. Override the text with `<data_dir>/agent-brief.md` |

### `github.*`

| Key | Default | Meaning |
|---|---|---|
| `github.intake` | `auto` | `auto` (webhook if `GITHUB_WEBHOOK_SECRET` is set, else poll), `webhook` (falls back to poll, with a startup warning, when the secret is missing), `poll`, `off` |
| `github.poll_interval_seconds` | `60` | Poll frequency in poll intake (must be positive) |
| `github.backfill_on_start` | `true` | Webhook intake: run one poll at startup to catch missed events |
| `github.backfill_lookback_hours` | `24` | How far back the first poll looks for a repository with no stored events |

### `routing.*`

| Key | Default | Meaning |
|---|---|---|
| `routing.ignore_targets` | `[]` | `for:`/`from:` label values that are people, not agents; never routed |
| `routing.notification_dedup_seconds` | `10` | Suppress the same issue → same agent notification within this window |

### `timing.*` — every threshold

| Key | Default | Meaning |
|---|---|---|
| `timing.stale_threshold_seconds` | `300` | Hook state older than this is verified against the terminal |
| `timing.snapshot_trust_seconds` | `20` | A stored state snapshot older than this is re-verified live |
| `timing.grace_period_seconds` | `5` | Settle time after an agent becomes idle before delivering (`settling`) |
| `timing.queue_expiry_minutes` | `30` | Queued messages older than this are expired |
| `timing.stall_threshold_seconds` | `5400` | Busy on one issue for longer than this is a stall |
| `timing.escalation_dedup_seconds` | `1800` | Do not repeat the same escalation within this window |
| `timing.monitor_interval_seconds` | `60` | `agent-monitor` job period (must be positive) |
| `timing.retry_interval_seconds` | `300` | `delivery-retry` job period (must be positive) |
| `timing.start_timeout_seconds` | `60` | How long `agent start` waits for the prompt |
| `timing.delivery_retention_days` | `30` | Deliveries and events older than this are pruned (every 6 h) |

### `telegram.*`

| Key | Default | Meaning |
|---|---|---|
| `telegram.allowed_chat_ids` | `[]` | **Required to enable the bot.** Chat ids (users or groups) allowed to issue commands |
| `telegram.notification_chat_id` | — | Where plan-waiting, dead-session and copy-mode alerts go |
| `telegram.group_chat_id` | — | Forum group used for topic routing (auto-discovered if omitted) |
| `telegram.topic_routes` | `{}` | `{"thread_id": "agent"}` mappings; `"agents"` is the catch-all topic |

### `escalation.*`

| Key | Default | Meaning |
|---|---|---|
| `escalation.target` | — | Agent that receives stall / dead-session / plan-waiting messages. Empty disables agent escalation (Telegram alerts still happen) |

### `priority.*` — "which issue is next"

Score = type weight + blocking bonus + dependents bonus + age tie-breaker.

| Key | Default |
|---|---|
| `priority.blocking_weight` | `1000.0` |
| `priority.type_weights` | `{"spec-gap": 100, "bug": 90, "task": 50, "question": 20, "optimization": 10}` |
| `priority.dependents_multiplier` | `1.5` |
| `priority.age_tiebreaker_weight` | `0.01` |

### `security.*`

| Key | Default | Meaning |
|---|---|---|
| `security.allow_remote_plan_control` | `false` | Enable approve/reject/respond on plans via API and Telegram (injects keystrokes) |
| `security.allow_remote_approval` | `true` | Let `agent approve` / `POST /agents/{name}/approve` answer a permission prompt. On by default because the action is bounded — a fixed affirmative key, sent only while the runtime's dialog is on screen, to a registered agent, recorded as an event — and because swarm coordinators need it to unblock members. Set `false` to keep every approval on a keyboard |
| `security.allow_unauthenticated` | `false` | Serve the API without an API key. Dev boxes only |

## Agents

Recorded per agent (`backbone agent list`, `GET /api/config/agents`):

| Field | Set by | Meaning |
|---|---|---|
| `name` | directory name, or `--name` | tmux session name, `for:`/`from:` identity |
| `dir` | `agent start --dir` | Working directory the runtime is started in |
| `runtime` | `--runtime` / `agent set` | `claude`, `codex`, `gemini`, `opencode`, `aider`, `cursor`, `shell` |
| `model` | `--model` / `agent set` | Passed as `--model` to the runtime |
| `repo` | `git remote origin` / `agent set` | `owner/name` the agent owns |
| `watches` | `agent watch` | Repositories it also hears about |
| `tags`, `description` | `agent set` | Free-form, returned by the API |
| `env` | `agent set env='{"K":"V"}'` | Extra environment exported into the session (e.g. an API key). Values are stored with the agent record — treat them like `.env` contents |

Exported into every session the backbone starts: `BACKBONE_RUNTIME`,
`BACKBONE_AGENT`, `BACKBONE_STATE_DIR` (an agent's `env` cannot override
these reserved keys). The API key is **not** exported.

## Secrets (`.env` / environment)

| Variable | Purpose |
|---|---|
| `BACKBONE_API_KEY` | Bearer token for the API (generated by `backbone init`) |
| `GITHUB_TOKEN` | PAT with `repo` scope, or `gh auth token` |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH` | GitHub App alternative to a token |
| `GITHUB_WEBHOOK_SECRET` | Webhook HMAC secret; setting it switches intake to webhook |
| `TELEGRAM_TOKEN` | Bot token |
| `BACKBONE_DATA_DIR` | Data directory (default `~/.local/share/agent-backbone`) |
| `BACKBONE_DATABASE_URL` | Any SQLAlchemy async URL, e.g. `postgresql+asyncpg://user:pw@host/db` (install the `postgres` extra) |

`backbone init` writes `.env` with mode 0600 and a fresh 32-byte API key.
