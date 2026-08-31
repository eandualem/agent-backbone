# Configuration

One TOML file plus a handful of environment variables.

## Where `backbone.toml` is found

In order: `BACKBONE_CONFIG=/path/to/backbone.toml`; a `backbone.toml` in the
current directory or any parent; `~/.config/agent-backbone/backbone.toml`;
otherwise built-in defaults (no agents, no integrations, SQLite in the data
dir). A `.env` next to the config file is loaded automatically.

`backbone init` writes a commented starter; [`backbone.example.toml`](../backbone.example.toml)
lists every key.

## `[backbone]`

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | `~/.local/share/agent-backbone` | SQLite database, hook state, pid files, poll checkpoint |
| `host` | `127.0.0.1` | Bind address. Keep it local unless you put auth and TLS in front. |
| `port` | `7120` | API port |
| `session_name` | `backbone` | tmux session used by `backbone up --detach` |
| `cors_origins` | `[]` | Browser origins allowed to call the API; empty disables CORS |
| `max_delivery_ids` | `100` | Size of the in-memory webhook/poll dedup cache (the database keeps the full log) |

## `[agents.<name>]`

One table per agent. The name must be a valid tmux session name (letters,
digits, `-`, `_`).

| Key | Required | Meaning |
|---|---|---|
| `dir` | yes | Working directory the CLI is started in |
| `runtime` | no (`claude`) | `claude`, `codex`, `gemini`, `opencode`, `aider`, `cursor`, `shell` |
| `model` | no | Passed as `--model` to the runtime |
| `repo` | no | `owner/name`. Issues and PRs in this repository route to this agent without labels; its open issues are part of the agent's queue |
| `tags` | no | Free-form strings, returned by the API for dashboards |
| `env` | no | Extra environment variables exported into the session |
| `description` | no | Free text, returned by the API |

## `[github]`

| Key | Default | Meaning |
|---|---|---|
| `repo` | — | Coordination repository (`owner/name`) where `for:<agent>` issues live. Leave unset to disable GitHub entirely |
| `mode` | `webhook` | `poll` or `webhook` |
| `poll_interval_seconds` | `30` | Poll frequency |

Credentials (environment): `GITHUB_TOKEN` **or** `GITHUB_APP_ID` +
`GITHUB_APP_PRIVATE_KEY_PATH`. `GITHUB_WEBHOOK_SECRET` is required in webhook
mode.

## `[routing]`

| Key | Default | Meaning |
|---|---|---|
| `ignore_targets` | `[]` | `for:`/`from:` label values that are people, not agents; never routed |
| `notification_dedup_seconds` | `10` | Window in which the same issue → same agent notification is suppressed (webhook retries, double closes) |

## `[delivery]`

| Key | Default | Meaning |
|---|---|---|
| `retention_days` | `30` | Delivery history retention (pruned every 6 h) |
| `grace_period_seconds` | `5` | Settle time after an agent becomes idle before delivering |
| `queue_retry_seconds` | `30` | Reserved for future use |

## `[monitor]`

| Key | Default | Meaning |
|---|---|---|
| `interval_seconds` | `60` | `agent-monitor` job period |
| `retry_interval_seconds` | `300` | `delivery-retry` job period |

## `[agent_state]`

| Key | Default | Meaning |
|---|---|---|
| `state_dir` | `<data_dir>/state` | Where hooks write `<agent>.json` and `actions.jsonl` |
| `stale_threshold_seconds` | `300` | Hook state older than this is verified against the terminal |

## `[escalation]`

| Key | Default | Meaning |
|---|---|---|
| `target` | — | Agent that receives stall / offline / plan-waiting messages. Empty disables agent escalation (Telegram notifications still happen) |
| `stall_threshold_seconds` | `5400` | Busy on one issue for longer than this counts as a stall |
| `dedup_seconds` | `1800` | Do not repeat the same escalation within this window |

## `[telegram]`

| Key | Default | Meaning |
|---|---|---|
| `allowed_chat_ids` | `[]` | **Required to enable the bot.** Chat ids (users or groups) allowed to issue commands |
| `notification_chat_id` | — | Where plan-waiting and copy-mode alerts are sent |
| `group_chat_id` | — | Forum group used for topic routing (auto-discovered if omitted) |
| `topic_routes` | `{}` | `thread_id = "agent"` mappings; `"agents"` is the catch-all topic |
| `topic_discovery_file` | `<data_dir>/telegram-topics.json` | Auto-discovered topic mappings |

Credential: `TELEGRAM_TOKEN`.

## `[priority_scoring]`

Controls "which issue is next". Score = type weight + blocking bonus +
dependents bonus + age tie-breaker.

| Key | Default |
|---|---|
| `blocking_weight` | `1000.0` |
| `type_weights` | `{ spec-gap = 100, bug = 90, task = 50, question = 20, optimization = 10 }` |
| `dependents_multiplier` | `1.5` |
| `age_tiebreaker_weight` | `0.01` |

## `[security]`

| Key | Default | Meaning |
|---|---|---|
| `allow_remote_plan_control` | `false` | Enable approve/reject/respond on plans via API and Telegram (injects keystrokes into the agent) |
| `allow_unauthenticated` | `false` | Serve the API without an API key. Dev boxes only |

## `[database]`

| Key | Default | Meaning |
|---|---|---|
| `url` | SQLite in `data_dir` | Any SQLAlchemy async URL, e.g. `postgresql+asyncpg://user:pw@host/db` (install the `postgres` extra) |
| `pool_size` / `pool_overflow` | `5` / `10` | Postgres pool sizing |
| `echo` | `false` | Log SQL |

## Environment variables

| Variable | Purpose |
|---|---|
| `BACKBONE_API_KEY` | Bearer token for the API (generated by `backbone init`) |
| `BACKBONE_CONFIG` | Explicit config path |
| `BACKBONE_DATA_DIR`, `BACKBONE_PORT`, `BACKBONE_DATABASE_URL` | Override the corresponding config keys |
| `BACKBONE_ALLOW_UNAUTHENTICATED` | Same as `[security] allow_unauthenticated` |
| `GITHUB_TOKEN` | PAT with `repo` scope, or `gh auth token` |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH` | GitHub App alternative |
| `GITHUB_WEBHOOK_SECRET` | Webhook HMAC secret |
| `TELEGRAM_TOKEN` | Bot token |

Exported into every agent session the backbone starts: `BACKBONE_RUNTIME`,
`BACKBONE_AGENT`, `BACKBONE_STATE_DIR`. The API key is **not** exported; give
it to agents deliberately (e.g. in their `env`) if they should call the API.
