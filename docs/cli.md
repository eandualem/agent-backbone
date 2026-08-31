# CLI reference

`backbone --help` lists everything; `-v` enables debug logging.

## `backbone init [--dir DIR] [--force]`

Writes `backbone.toml` (commented starter with one example agent) and `.env`
(with a generated `BACKBONE_API_KEY`, mode 0600) into `DIR` (default: current
directory). Refuses to overwrite without `--force`.

## `backbone doctor`

Checks and prints ✓/✗ for: config found, agents configured, each agent's
directory and runtime binary, tmux on PATH, API key configured, GitHub
credentials and webhook secret (when GitHub is configured), Telegram
allowlist (when a token is set). Exit code 1 if anything failed.

## `backbone up [--detach] [--reload]`

Runs the backbone: API, Socket.IO, background jobs, Telegram bot, GitHub
connector — one process.

- `--detach` runs it inside a tmux session (`[backbone] session_name`,
  default `backbone`); `backbone down` stops it.
- `--reload` restarts on code changes (development).

## `backbone down`

Gracefully stops a detached backbone (SIGTERM to the process in the tmux
session, then kill after 15 s).

## `backbone status`

Shows whether the API is up and each component's health, every configured
agent with `running`/`stopped`, and any other tmux sessions.

## `backbone agent …`

| Command | Effect |
|---|---|
| `agent list` | Configured agents with runtime, model and directory |
| `agent start NAME [--runtime R] [--model M] [--resume]` | Start the agent's tmux session (idempotent). Flags override the config for this launch |
| `agent stop NAME` | Kill the session |
| `agent start-all` / `agent stop-all` | All configured agents |

`agent start` does not go through the API, so it works while the backbone is
down. Hooks still get `BACKBONE_AGENT` and `BACKBONE_STATE_DIR`.

## `backbone tell AGENT MESSAGE… [--from NAME] [--priority]`

Delivers a message through the running backbone (`POST /api/messages`) and
prints the outcome JSON. `--from` defaults to your username. `--priority`
lets the message through while a human is typing in the session. Exit code 0
if delivered, 2 if it was queued/refused, 1 on API errors.

## `backbone hooks install|uninstall claude [--dir PROJECT]`

Copies the hook script into `<data_dir>/hooks/` and adds tagged entries to
`~/.claude/settings.json` (or `PROJECT/.claude/settings.json` with `--dir`).
Re-running is idempotent; `uninstall` removes only the backbone's entries.
Restart running Claude Code sessions afterwards.
