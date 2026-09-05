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
  The running backbone publishes the new configuration immediately. Routing
  and delivery thresholds use it on their next operation. Restart the backbone
  after changing server bindings (`backbone.host`, `backbone.port`,
  `backbone.cors_origins`), GitHub intake/backfill mode, or job periods
  (`github.poll_interval_seconds`, `timing.monitor_interval_seconds`,
  `timing.retry_interval_seconds`): listeners and scheduled jobs are constructed
  at startup. Enabling an integration that was disabled at startup also requires
  a restart.
- **Agents** are discovered by `backbone agent start` and edited with
  `backbone agent set|watch|unwatch|forget`.
- **Secrets** come from `<data_dir>/.env` (read at startup into the config
  snapshot, never into the process environment — otherwise the tmux server
  the daemon spawns would hand them to every agent session) or the
  environment, which wins. Only that one file is read — a `.env` in the current
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
| `backbone.restart_on_upgrade` | `true` | Restart the running backbone onto new code when the installed version (or, for a development checkout, the commit of the branch it started on) changes. Checked once a minute; waits until nothing is being routed; a checkout switched to another branch is left alone. Agents are untouched |

### `agents.*`

| Key | Default | Meaning |
|---|---|---|
| `agents.default_runtime` | `claude` | Runtime used by `agent start` when none is given |
| `agents.pre_trust` | `true` | Answer the runtime's folder-trust dialog before starting, so it never blocks an unattended start: Claude Code and Codex get the same trust record their own dialog writes; Gemini is launched with `--skip-trust`. Starting an agent in a directory is treated as the trust decision; set `false` to answer the dialog yourself |
| `agents.writable_dirs` | `[]` | Machine-wide directories that every Codex agent may write outside its own checkout (`--add-dir`; JSON list, `~` allowed). Use for deliberately shared tooling caches; for a project-specific cache, set `UV_CACHE_DIR` inside the agent's worktree. Other runtimes ignore this setting. See [permission boundaries and cache options](security.md#unattended-agents-and-writable-directories) |
| `agents.auto_review` | `false` | Use automatic permission review where the runtime supports it (currently Codex, `--approve-for-me`, with its workspace sandbox). Routine requests can proceed after review; refusals return to the agent. Applies on the next start/resume. Unattended agents keep their no-prompt policy; other runtimes are unaffected. Set `false` to use your own runtime approval configuration |
| `agents.inject_brief` | `true` | Give each agent the backbone's common brief at launch — who it is, how to message other agents, and where to get details (`backbone help`). Claude Code appends it to the system prompt (complementing the project's CLAUDE.md); Codex, Gemini and OpenCode receive it as the session's initial prompt (not re-sent on `--resume`); `aider` receives it as its first delivered message; plain shells get none. Override the text with `<data_dir>/agent-brief.md` |

### `github.*`

| Key | Default | Meaning |
|---|---|---|
| `github.intake` | `auto` | `auto` (webhook if `GITHUB_WEBHOOK_SECRET` is set, else poll), `webhook` (falls back to poll, with a startup warning, when the secret is missing), `poll`, `off` |
| `github.poll_interval_seconds` | `60` | Poll frequency in poll intake (must be positive) |
| `github.backfill_on_start` | `true` | Webhook intake: run one poll at startup to catch missed events |
| `github.backfill_lookback_hours` | `24` | How far back the first poll looks for a repository with no durable poll cursor (including the first start after upgrading to cursor storage) |

### `routing.*`

| Key | Default | Meaning |
|---|---|---|
| `routing.ignore_targets` | `[]` | `for:`/`from:` label values that are people, not agents; never routed |
| `routing.notification_dedup_seconds` | `10` | Suppress the same issue → same agent notification within this window |

### `timing.*` — every threshold

| Key | Default | Meaning |
|---|---|---|
| `timing.stale_threshold_seconds` | `300` | Hook state older than this is verified against the terminal |
| `timing.grace_period_seconds` | `5` | Settle time from the hook-written idle timestamp before delivering (`settling`); terminal-only idle readings have no transition timestamp |
| `timing.queue_expiry_minutes` | `30` | Queued messages older than this are expired |
| `timing.stall_threshold_seconds` | `5400` | Busy on one issue for longer than this is a stall |
| `timing.escalation_dedup_seconds` | `1800` | Do not repeat the same escalation within this window |
| `timing.monitor_interval_seconds` | `60` | `agent-monitor` job period (must be positive) |
| `timing.retry_interval_seconds` | `300` | `delivery-retry` job period (must be positive) |
| `timing.start_timeout_seconds` | `60` | How long `agent start` waits for the prompt |
| `timing.delivery_retention_days` | `30` | Deliveries, events and completed queue messages are pruned every 6 h; queue age is measured from completion, and pending/leased messages are retained |

### `telegram.*`

| Key | Default | Meaning |
|---|---|---|
| `telegram.allowed_chat_ids` | `[]` | **Required to enable the bot.** Chat ids (users or groups) allowed to issue commands |
| `telegram.notification_chat_id` | — | Where plan-waiting, dead-session and copy-mode alerts go |
| `telegram.group_chat_id` | — | Forum group where each agent gets a topic (learned from the first message in the group if omitted) |
| `telegram.auto_topics` | `true` | Create a forum topic per registered agent in that group (swarm members excepted: a `swarm:<name>` tag means no topic), close it when the agent is forgotten, reopen it if it returns. Needs the bot as an administrator with *Manage Topics*. `false` to manage topics yourself |
| `telegram.topic_routes` | `{}` | Explicit `{"thread_id": "agent"}` mappings on top of the automatic ones (never closed automatically); `"agents"` is the catch-all topic |

### `escalation.*`

| Key | Default | Meaning |
|---|---|---|
| `escalation.target` | — | Agent that receives stall / offline / plan-waiting messages. Empty disables agent escalation (Telegram alerts still happen) |

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
| `security.allow_remote_approval` | `true` | Let `agent approve` / `POST /api/agents/{name}/approve` answer a permission prompt. On by default because the action is bounded — a fixed affirmative key, sent only while the runtime's dialog is on screen, to a registered agent, recorded as an event — and because swarm coordinators need it to unblock members. Set `false` to keep every approval on a keyboard |
| `security.allow_unauthenticated` | `false` | Serve the API without an API key. Dev boxes only |

### `swarm.*`

| Key | Default | Meaning |
|---|---|---|
| `swarm.unattended_members` | `true` | Run sandboxed swarm members unattended (currently Codex). Evaluated at every member start from the current setting and runtime; not persisted on the member. Other runtimes retain their approval policy. See [permission boundaries](security.md#unattended-agents-and-writable-directories) |

## Agents

Recorded per agent (`backbone agent list`, `GET /api/config/agents`):

| Field | Set by | Meaning |
|---|---|---|
| `name` | directory name, or `--name` | tmux session name, `for:`/`from:` identity |
| `dir` | `agent start --dir` | Working directory the runtime is started in |
| `runtime` | `--runtime` / `agent set` | `claude`, `codex`, `gemini`, `opencode`, `deepcode`, `aider`, `shell` |
| `model` | `--model` / `agent set` | Passed as `--model` to the runtime |
| `repo` | `git remote origin` / `agent set` | `owner/name` the agent owns |
| `watches` | `agent watch` | Repositories it also hears about |
| `tags`, `description` | `agent set` | Free-form, returned by the API. A `swarm:<name>` tag marks a swarm member: internal to the agent running the swarm, no Telegram topic |
| `always_on` | `agent set NAME always_on=true` | Expected to stay up: a dead session is reported at once. Off by default — an absent agent is reported only when messages are queued for it |
| `unattended` | `agent set NAME unattended=true` | Select the runtime's no-approval mode. Off by default for ordinary agents; sandboxed swarm members follow `swarm.unattended_members` at launch. Changing `runtime` clears this flag unless set again in the same command. Unsupported runtimes refuse unattended startup. See [runtime switches, permission boundaries and Claude's one-time acceptance](security.md#unattended-agents-and-writable-directories) |
| `env` | `agent set env='{"K":"V"}'` | Extra environment exported into the session (e.g. an API key). Values are stored with the agent record — treat them like `.env` contents |

Exported into every session the backbone starts: `BACKBONE_RUNTIME`,
`BACKBONE_AGENT`, `BACKBONE_STATE_DIR` (an agent's `env` cannot override
these reserved keys). That is the whole contract — the backbone's own
secrets are stripped from the session, including anything you added to
`.env` yourself. See
[What an agent session inherits](security.md#what-an-agent-session-inherits).

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

`backbone init` writes `.env` with mode 0600 and a fresh 32-byte API key;
`backbone secrets set KEY` adds to it (prompted, or `set KEY VALUE`),
`backbone secrets path` prints where it is. **Why not a `.env` in the
repository?** The backbone is installed once and runs `agent start` inside
many repositories, each with its own `.env` for its own app — reading
those would leak unrelated secrets into agent sessions. So exactly one
file is read, and it is the data directory's — and its contents are kept
out of agent sessions rather than exported into them.
