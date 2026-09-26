# Configuration

Settings live in the database, secrets in `.env`, and editable startup
instructions in Markdown files. The default **data directory** contains:

```
~/.local/share/agent-backbone/      # $BACKBONE_DATA_DIR to move it
├── .env            secrets only (API key, tokens) — never in the database
├── backbone.db     settings, agents, watches, events, deliveries, queue, state
├── state/          hook-written agent state, actions.jsonl, plans/
├── hooks/          installed runtime hooks
└── templates/      editable instructions (unless templates.dir relocates them)
```

- **Settings** are keys with built-in defaults, stored in the database and
  edited with `backbone config set KEY VALUE` (or `PUT /api/config/{key}`).
  The running backbone publishes the new configuration immediately. Routing
  and delivery thresholds use it on their next operation. Job periods and
  GitHub poll intake reconcile on publication: a sleeping job resets its timer;
  an active run finishes before a disabled job stops. Integration connections
  start or stop as their required settings change. Restart for server bindings
  (`backbone.host`, `backbone.port`, `backbone.cors_origins`) and GitHub credentials.
  Webhook backfill remains a startup-only operation; changing its setting does
  not replay historical activity immediately.
- **Agents** are discovered by `backbone agent start` and edited with
  `backbone agent set|watch|unwatch|forget`.
- **Secrets** come from `<data_dir>/.env` (read at startup into the config
  snapshot, never into the process environment — otherwise the tmux server
  the daemon spawns would hand them to every agent session) or the
  environment, which wins. Only that one file is read — a `.env` in the current
  working directory is ignored.

Use environment variables to select storage: `BACKBONE_DATA_DIR` (where it
is) and `BACKBONE_DATABASE_URL` (PostgreSQL instead of the SQLite file).

Persistent database startup checks that required tables and columns exist,
even when the migration revision is current. Missing structures trigger
schema repair while preserving stored rows. A complete current schema does
not rebuild its indexes on every start.

## Settings

`backbone config list` prints every key with its current value and marks
the ones you changed. Values are JSON (`7999`, `true`, `'["a","b"]'`,
`'{"42":"reviewer"}'`); plain strings need no quoting.

### `usage.*`

- `usage.enabled` (default `true`): import numeric usage from identified runtime
  sessions on the existing monitor and query refresh. Disabling retains history.
- `usage.prices`: a JSON object keyed by exact observed model ID. The bundled
  catalog contains sourced standard API rates dated 2026-09-12. Each entry has
  `as_of`, `source`, and USD per million `input`, `cache_read`, `cache_write`,
  `cache_write_1h`, `output`; optional `long_context_above`,
  `long_input_multiplier`, `long_output_multiplier` define context pricing.
  Rates must be nonnegative. Setting this object replaces the catalog; omitted
  models remain unpriced. Keep existing entries when extending it. Stored
  historical price bases remain unchanged; `usage --reprice --cost` previews
  the configured catalog against retained tokens.

See [Token usage](token-usage.md) for sources, coverage and cost semantics.

### `backbone.*`

| Key | Default | Meaning |
|---|---|---|
| `backbone.host` | `127.0.0.1` | Bind address; IPv6 loopback is `::1` (the CLI adds URL brackets). Keep it local unless you put auth and TLS in front |
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
| `agents.shared_policy` | `[]` | Ordered policy names under `<templates.dir>/policies/`, composed with base and swarm briefs; see [templates](templates.md) |
| `agents.tag_policy` | `{}` | Policy names by agent tag, as a JSON object of ordered lists; global policies apply first, then matching tags alphabetically, deduplicated |
| `agents.inject_brief` | `true` | Give each agent the backbone's common brief at launch — who it is, how to message other agents, and where to get details (`backbone help`). Claude Code appends it to the system prompt (complementing the project's CLAUDE.md); Gemini and OpenCode receive it as the session's initial prompt; Codex and `aider` receive it as their first delivered message (a Codex launch command is capped at 16 KB by tmux, so the brief cannot ride on it; a brief an earlier launch never delivered is dropped at the next launch); plain shells get none. A resumed session keeps the brief it started with on every runtime (Claude Code keeps its stored system prompt). On resume, Claude Code and Codex are handed the current brief through their `SessionStart` hook, saying that it supersedes earlier Backbone briefs; on the other runtimes a changed brief applies only to a fresh start (`agent start --fresh`). Override the text with `base.md` in `templates.dir` (`backbone templates edit base`) |

### `skills.*`

| Key | Default | Meaning |
|---|---|---|
| `skills.store` | `~/skills` | Directory holding the one copy of every shared skill (an absolute path; `~` is expanded). At launch each agent gets the skills tagged for it as symlinks in the directory its CLI reads; `backbone skills` manages the store. Empty disables materialisation. See [Skills](skills.md) |

### `templates.*`

| Key | Default | Meaning |
|-----|---------|---------|
| `templates.dir` | `""` | Your configuration directory: edited `base.md`, `swarm/` and `policies/` are read from and written to it (`backbone templates`). Empty means `<data_dir>/templates`. Set an absolute path outside the data dir to keep it as a git repository of its own — no secrets, database or logs live there; move the existing files there first. See [templates](templates.md) |
| `templates.maintainer` | `""` | Who maintains the shared policies (an agent name or a person); the base brief tells agents to propose policy changes there. Empty renders as "the owner of this backbone" |

### `github.*`

| Key | Default | Meaning |
|---|---|---|
| `github.review_poll_interval_seconds` | `300` | Minimum interval between review metadata polls per repo, with a separate durable cursor; positive seconds |
| `github.reviewers` | `[]` | Reviewer logins or GitHub App slugs (with or without `[bot]`); replace their PR comments with commit-anchored review lifecycle notices. Enables review polling; see [GitHub](github.md#review-lifecycle) |
| `github.intake` | `auto` | `auto` (webhook if `GITHUB_WEBHOOK_SECRET` is set, else poll), `webhook` (falls back to poll, with a startup warning, when the secret is missing), `poll`, `off` |
| `github.poll_interval_seconds` | `60` | Poll frequency in poll intake (must be positive) |
| `github.backfill_on_start` | `true` | Webhook intake: start catch-up at startup; the delivery-retry job retries unfinished repositories until complete |
| `github.backfill_lookback_hours` | `24` | How far back the first poll looks for a repository with no durable poll cursor (including the first start after upgrading to cursor storage) |

### `sources.*`

| Key | Default | Meaning |
|---|---|---|
| `sources.poll_interval_seconds` | `60` | How often subscribed [sources](sources.md) (Gmail) are searched for new items matching subscriptions; the job runs only when a source is configured (must be positive) |

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
| `timing.queue_expiry_minutes` | `30` | Expiry for ordinary queued messages; active swarm coordination, inbox holds, startup briefs, queues held by an unconfirmed delivery and [subscription](sources.md) batches are retained. Recipients and registered senders are notified of what expired |
| `timing.stall_threshold_seconds` | `5400` | Busy on one issue for longer than this is a stall |
| `timing.escalation_dedup_seconds` | `1800` | Do not repeat the same escalation within this window |
| `timing.monitor_interval_seconds` | `60` | `agent-monitor` job period (must be positive) |
| `timing.retry_interval_seconds` | `300` | `delivery-retry` job period (must be positive) |
| `timing.start_timeout_seconds` | `60` | How long `agent start` waits for the prompt |
| `timing.delivery_retention_days` | `30` | Deliveries, events, completed queue messages, diagnostics and report history are pruned every 6 h; diagnostics age from their last observation, queue age from completion; pending/leased messages and each author's latest progress report are retained |

### `telegram.*`

| Key | Default | Meaning |
|---|---|---|
| `telegram.allowed_chat_ids` | `[]` | **Required to enable the bot.** Chat ids (users or groups) allowed to issue commands |
| `telegram.notification_chat_id` | — | Where plan-waiting and dead-session alerts go |
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
| `inbox_only` | `agent start --inbox-only` / `agent set NAME inbox_only=true` | A client without a terminal, such as a service that reads the API: registered but never launched, restarted, stopped or typed into (approve, deny, steer and plan answers are refused). It reads its messages with `backbone inbox --agent NAME`; direct messages to it wait there without expiring, and it raises no offline alerts. It takes no part in GitHub routing: no `repo`, no watches, and `for:`/`from:` labels naming it are ignored. A running agent is stopped before it can be made inbox-only; `always_on`, `unattended` and subscriptions do not apply, and a swarm member cannot be inbox-only. Off by default |
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
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | The mailbox and its app password for the read-only Gmail [source](sources.md); both unset means no mailbox |
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

## Shared policy and environment facts

Use `backbone templates edit policy:NAME` to create a policy, then
`backbone templates use NAME` to assign it globally, or add `--tag TAG` for a group.
Assignments are stored in the database; Markdown files live in
`<templates.dir>/policies/` (default `<data_dir>/templates/policies/`). `backbone templates preview AGENT` shows the
actual next-launch content and sources. Missing or empty selected rules fail
launch, including when the base brief is customized. See [Agent instruction
templates](templates.md) for ordering, migration from legacy paths, placeholders,
and the distinction between fresh starts and resumed conversations.

## Agent record validation

Registration, API edits and CLI direct edits share store validation for runtime,
model/effort syntax, repository names, paths and field shapes. Model IDs remain
open-ended; known effort levels are checked against the runtime. Directories
need not exist when recorded but must exist at launch. Empty `repo` removes
ownership; watches require `OWNER/REPO`. Invalid updates write no fields.
Existing records remain visible on refresh; repair invalid fields together before
editing or starting them. No agent is silently deleted or migrated to a runtime.

### Progress notifications

`telegram.report_updates` defaults to `true`. Newly published reports are queued
in the database and posted to the allowlisted agents group every 30 seconds.
Set it to `false` to pause sending without deleting reports; setting it back to
`true` drains pending reports still within retention. No group means delivery waits.
Destinations are General and the agent’s own topic, not the private alert chat. Reports
saved before this feature are never automatically broadcast.

Reports now appear in both General and the agent's topic, with separate delivery
receipts. Optional full-report voice messages can be enabled with
`backbone config set telegram.report_audio true` after local speech setup.
See `backbone docs report-audio` for the model, service, voice and FFmpeg setup.



See [message checkpoints](cli.md#cooperative-message-checkpoints) for safe mid-turn
coordination, acknowledgement and retention.
