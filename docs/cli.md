# CLI reference

`backbone --help` lists everything; `ab` is the same command under a short
name; `-v` enables debug logging. Commands go
through the running backbone's API when it is up and fall back to the
database (and tmux) directly when it is not.

## `backbone init [--data-dir DIR] [--force]`

Creates the data directory with `.env` (generated `BACKBONE_API_KEY`, mode
0600), the SQLite database (migrated), and `state/`. Keeps an existing
`.env` unless `--force`.

## `backbone doctor`

Checks and prints ✓/✗ for: data dir and `.env`, database reachable, each
known agent's directory and runtime binary, tmux on PATH, installed
runtimes, API key, GitHub credentials and effective intake, Telegram
allowlist, whether the API is up. Exit code 1 if anything failed.

## `backbone up [--detach] [--reload]` · `backbone down`

Runs the backbone: API, Socket.IO, background jobs, Telegram bot, GitHub
intake — one process. `--detach` runs it inside a tmux session
(`backbone.session_name`); `down` stops it gracefully. `--reload` restarts
on code changes (development).

Nothing starts at boot by itself — after a reboot, `backbone up --detach`.
To start it at login on macOS, install a LaunchAgent once:

```bash
cat > ~/Library/LaunchAgents/dev.agent-backbone.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.agent-backbone</string>
  <key>ProgramArguments</key><array>
    <string>$HOME/.local/bin/backbone</string><string>up</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.local/share/agent-backbone/backbone.log</string>
  <key>StandardErrorPath</key><string>$HOME/.local/share/agent-backbone/backbone.log</string>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/dev.agent-backbone.plist
```

## `backbone status`

API health per component, GitHub intake mode, every known agent with its
live state (`idle`, `busy`, `waiting_for_human(plan)`, `offline`, …),
runtime, repository and directory, other tmux sessions, and every tracked
repository with its owners, watchers and the last event seen.

## `backbone config list | get KEY | set KEY VALUE | unset KEY`

Settings live in the database with built-in defaults; see
[Configuration](configuration.md). `set` validates the value and applies
it to the running backbone at once. Values are JSON where needed:

```bash
backbone config set timing.grace_period_seconds 3
backbone config set telegram.allowed_chat_ids '[123456789]'
backbone config set escalation.target orch
```

## `backbone agent …`

| Command | Effect |
|---|---|
| `agent start [NAME] [--dir D] [--name N] [--runtime R] [--model M] [--resume] [--watch REPO]… [--no-wait]` | Discover the agent from `--dir` (default: cwd when no name is given), record it, start its tmux session and **wait until it is at its prompt**. With a bare `NAME` the agent must already be known |
| `agent list` | Known agents with runtime, model and directory |
| `agent inspect NAME [--json]` | State, reason, current issue, delivery condition, the evidence, the terminal tail, recent deliveries |
| `agent stop NAME` | Kill the session |
| `agent set NAME key=value…` | Change `dir`, `runtime`, `model`, `repo`, `description`, `tags` (JSON list), `env` (JSON object) |
| `agent watch NAME REPO…` / `agent unwatch NAME REPO…` | Add / remove watched repositories |
| `agent forget NAME` | Remove a stopped agent from the backbone |
| `agent start-all` / `agent stop-all` | Every known agent |

`agent start` reports one of:

```
app: ready — claude repo acme/app                      # at its prompt
app: started, waiting for you — claude repo acme/app   # e.g. Claude's folder-trust question; tmux attach -t app
app: started but not at its prompt yet                 # timeout; the last terminal lines are shown
app: already running
```

## `backbone tell AGENT MESSAGE… [--from NAME] [--priority]`

Delivers `[via:backbone from:NAME] MESSAGE` through the running backbone
(`POST /api/messages`) and prints the outcome JSON. `--priority` lets the
message through while a human is typing or the agent is settling; it never
interrupts a busy agent. Exit code 0 if delivered, 2 if queued, 1 on API
errors.

## `backbone hooks install|uninstall claude [--dir PROJECT]`

Copies the hook script into `<data_dir>/hooks/` and adds tagged entries to
`~/.claude/settings.json` (or `PROJECT/.claude/settings.json`). Re-running
is idempotent; `uninstall` removes only the backbone's entries. The hook
prefers `$BACKBONE_STATE_DIR` (exported into every session the backbone
starts), so one global install serves any data directory. Restart running
Claude Code sessions afterwards.
