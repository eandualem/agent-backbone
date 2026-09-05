# CLI reference

`backbone --help` lists everything; `ab` is the same command under a short
name; `-v` enables debug logging. Commands go
through the running backbone's API when it is up and fall back to the
database (and tmux) directly when it is not — except `agent approve` and
`tell`, which only work through the API so that every keystroke into an
agent is audited.

## `backbone init [--data-dir DIR] [--force]`

Creates the data directory with `.env` (generated `BACKBONE_API_KEY`, mode
0600), the SQLite database (migrated), and `state/`. Keeps an existing
`.env` unless `--force`.

## `backbone secrets set KEY [VALUE] | unset KEY | list | path`

The one `.env` the backbone reads lives in the data directory
(`~/.local/share/agent-backbone/.env` by default), not in any repository:
the backbone runs `agent start` *inside* your projects, which have `.env`
files of their own, so it deliberately reads only its own. `set` writes a
value (prompted when omitted, so it never enters shell history; a `# KEY=`
placeholder from `init` is filled in place; mode stays 0600), `unset`
removes one, `list` shows which known secrets are present (names only),
`path` prints the file. The running backbone reads it at startup —
`backbone down && backbone up --detach` after a change.

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

`backbone up --detach` is manual: a tmux session ends with a reboot. To
have the backbone start at login and restart if it dies, install the
login service once:

```bash
backbone service install     # macOS: a LaunchAgent; Linux: a systemd --user unit
backbone service status      # running | installed | not installed | unsupported
backbone service restart     # what `backbone upgrade` does after upgrading
backbone service uninstall
```

Where there is no launchd or `systemd --user` — a container, a minimal
image — `install` says so and leaves nothing behind; `status` reports
`unsupported`. Run `backbone up --detach` there instead (and again after
a restart).

The service runs `backbone up` in the foreground with the data directory
you installed it from; its log is `<data_dir>/backbone.log` on macOS and
`journalctl --user -u agent-backbone` on Linux. Agents are still tmux
sessions and still need `backbone agent start` after a reboot.

## `backbone upgrade [--check] [--no-restart]`

New code in, one restart, agents untouched. Upgrades the package through
the installer that put it there (`uv tool upgrade agent-backbone` or
`pipx upgrade agent-backbone`; a development checkout runs whatever is
checked out, so nothing is downloaded), then restarts the backbone: the
login service when it is installed, otherwise the `backbone up --detach`
tmux session. Waits for the API to answer and prints the running version.
Agents are tmux sessions and the queue is in the database, so the restart
loses nothing but a few seconds of API. `--check` only reports the
installed version and the newest on PyPI.

The running backbone also restarts itself: once a minute it compares the
code on disk with what it started as and, when they differ and nothing is
being routed, re-executes `backbone up` in place
(`backbone.restart_on_upgrade`, on by default). So `uv tool upgrade`, or
pulling the checkout, is enough; `backbone upgrade` is the same thing
done now. A development checkout switched to another branch is left
alone — that is development, not an upgrade; the restart happens when
the branch the backbone started on moves. `backbone service restart` is
the plain building block.

## `backbone runtimes`

Every runtime the backbone knows, whether its binary is on `PATH`, and
example model ids that work with `--model` (Claude Code's aliases, the id
Codex shows in its status line, Deep Code's two models). The list is a
starting point for agents choosing a model for another agent or a swarm
member; the runtime's own model picker is the authority.

Each line also prints the reasoning-effort levels that runtime accepts —
`low, medium, high, xhigh, max` for Claude Code, the same plus `ultra`
for Codex, `-` for a CLI with no effort setting. Unlike model ids, these
are checked: a level the runtime does not have is refused at start.

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
| `agent start [NAME…] [--dir D] [--name N] [--runtime R] [--model M] [--resume] [--watch REPO]… [--no-wait]` | Discover the agent from `--dir` (default: cwd), record it, start its tmux session and **wait until it is at its prompt**. A bare known `NAME` starts from its recorded directory; a bare unknown `NAME` registers the cwd under that name. Several names start a group of known agents (`ab agent start app web orch`) |
| `agent list` | Known agents with runtime, model and directory |
| `agent inspect NAME [--json]` | State, reason, current issue, delivery condition, the runtime's session id and the agent's last reply (when its hook reports them), the evidence, the terminal tail, recent deliveries |
| `agent stop NAME…` | Kill the session(s) |
| `agent approve NAME [--from WHO]` | Answer the permission prompt the agent's runtime is showing (Claude Code, Codex, OpenCode — each verified against a live dialog; other runtimes report `unsupported`). Checks the terminal at the moment of the call and types only if the dialog is on screen *then* — otherwise reports `not_waiting` with the terminal tail. (tmux has no check-and-send: a dialog a human answers in that same instant can receive one extra key at an empty prompt; the response says whether the dialog actually cleared.) Needs the backbone running (`backbone up`): there is no direct-tmux fallback, so every approval goes through the API and is recorded as an `approval` event. Disable with `security.allow_remote_approval false` |
| `agent set NAME key=value…` | Change `dir`, `runtime`, `model`, `repo`, `description`, `tags` (JSON list), `env` (JSON object), `always_on` (`true`/`false`) |
| `agent watch [NAME] REPO…` / `agent unwatch [NAME] REPO…` | Add / remove watched repositories. Inside an agent session `NAME` defaults to the agent itself (`$BACKBONE_AGENT`), so an agent can subscribe on its own |
| `agent forget NAME` | Remove a stopped agent from the backbone (refuses while its session is still running) |

### Every `agent start` parameter

`agent start` is the whole declaration — there is no separate registration
step, and everything is optional except a directory to discover (given, or
the cwd). Anyone with a shell can drive it, **including another agent**:
an orchestrator that should spin up workers runs these commands itself.

| Parameter | Meaning | Recorded on the agent? |
|---|---|---|
| `NAME` (positional) | Agent name = tmux session = `for:` label. Known name: starts from its recorded directory. Unknown name: registers the cwd under it. Omitted: the folder name is the name — the usual case for single-repository agents | yes (the key) |
| `--dir D` | Project directory to discover (name defaults from its folder name; repo from its `origin` remote) | yes |
| `--runtime R` | Which CLI runs the agent: `claude` (default via `agents.default_runtime`), `codex`, `gemini`, `opencode`, `deepcode`, `aider`, or `shell` | yes — later bare starts reuse it |
| `--model M` | Passed to the runtime as `--model M` (e.g. `opus`, `sonnet`, or a full model id — whatever that CLI accepts). Use it to run cheaper models per agent. Write it as `M:EFFORT` (e.g. `gpt-6-astra:high`, `opus:max`) to set the reasoning effort too — the backbone translates the level into that CLI's own switch, and refuses a level the runtime does not have rather than dropping it | yes — later bare starts reuse it |
| `--watch OWNER/REPO` | Also subscribe to a repository (repeatable) | yes |
| `--resume` | Reopen the session the backbone last saw through the runtime's hook (its id is recorded), else the runtime's last conversation | no |
| `--always-on` | Instead of names: start every agent marked `always_on` (after a reboot, with `--resume`) | — |
| `--no-wait` | Return immediately instead of waiting for the prompt | no |

Examples:

```bash
backbone agent start                                  # this repo, defaults
backbone agent start --model opus                     # this repo, cheaper model
backbone agent start orch --dir ~/ws/orch --watch acme/app
backbone agent start --dir ~/ws/api --runtime codex --model gpt-5.2
backbone agent start recruiter-desk                   # known agent, recorded settings
```

Recorded settings are changed later with `agent set NAME model=sonnet`
(or `runtime=…`, `dir=…`), and a one-off override at start
(`agent start NAME --model haiku`) also updates the record.

Moving a project: registration is keyed by name (default: the folder name).
Starting a known name from a new directory updates the record **if the old
directory is gone** — the agent follows the move, keeping its watches and
settings. If the old directory still exists, the new one is a different
project that happens to share a folder name and is registered as `name-2`.
A changed folder name is a new agent; `agent forget` removes the old one.

`agent start` reports one of:

```
app: ready — claude repo acme/app                      # at its prompt
app: started, waiting for you — claude repo acme/app   # e.g. Claude's folder-trust question; tmux attach -t app
app: started but not at its prompt yet                 # timeout; the last terminal lines are shown
app: already running
```

## `backbone swarm create|list|status|disband`

Run a coordinator+members swarm on one existing issue — see
[Swarms](swarms.md). `create NAME --issue OWNER/REPO#N [--member SPEC]…`
starts the roster in a shared worktree; `disband NAME` stops the members
and removes the worktree (the branch is kept). The swarm's issue being
closed (normally by merging the coordinator's PR) tears it down
automatically. `backbone tell <swarm-name> …` reaches its coordinator.

## `backbone help [TOPIC]`

The backbone explains its own capabilities to agents: no argument lists
the topics (`setup`, `agents`, `messaging`, `swarms`, `github`, …), a
topic name prints the full playbook. Also served at
`GET /api/help[/{topic}]`. `setup` is the one written for an agent that
is installing the backbone for a person: install, run, first agent,
GitHub, Telegram, and exactly where a human is needed.
Every backbone-started Claude agent carries a short injected brief (see
`agents.inject_brief`) that points here, so the injected text stays
small while the capability surface can grow. Add or override topics by
dropping markdown files into `<data_dir>/help-topics/`.

## `backbone docs [PAGE]`

The documentation in this directory, from the installed package: no
argument lists the pages with their titles, a page name
(`getting-started`, `concepts`, `how-it-works`, `cli`, …) prints it.
The wheel ships `docs/` inside the package so an agent that installed the
backbone from PyPI can read the reference without a checkout; a source
checkout reads the same files from the repository. Also served at
`GET /api/docs[/{page}]`.

## `backbone tell AGENT MESSAGE… [--from NAME] [--priority]`

Delivers `[via:backbone from:NAME] MESSAGE` through the running backbone
(`POST /api/messages`) and prints the outcome JSON. The sender defaults to
`$BACKBONE_AGENT` (set in every backbone-started session), so an agent's
messages are attributed to the agent, not the human account; `--from`
overrides it. `--priority` lets the
message through while a human is typing or the agent is settling; it never
interrupts a busy agent — that is an invariant, not a gap. A message that
cannot be delivered now (`agent_working`, `offline`, …) is stored in the
queue and the monitor delivers it when the agent is ready, oldest first;
queued messages expire after `timing.queue_expiry_minutes` (default 30).
The reply prints one sentence saying what happened: delivered; stored
(`"queued": true`); the same message from you already waiting
(`"queue": "already_queued"` — nothing was added); or not stored
(`"queue": "failed"` — send again later). Exit code 0 if delivered, 2 if
the message is in the queue, 1 if it is not (API error or storage
failure). Multi-line messages are pasted with bracketed paste and arrive
intact as a single message.

## `backbone reply TEXT… [--agent NAME]`

The other direction: an agent answers the humans on the channel they use.
`POST /api/integrations/reply` posts the text into the agent's surface on
every enabled integration — on Telegram, the forum topic mapped to that
agent. Inside an agent session `--agent` defaults to `$BACKBONE_AGENT`.
Exit 1 with the reason when no integration is configured or none has a
surface for the agent yet. See [Integrations](integrations.md).

## `backbone hooks install|uninstall claude|codex|gemini [--dir PROJECT]`

Sessions started by `agent start` need no install: the backbone wires its
hooks into every launch it performs (Claude Code `--settings`, Codex `-c`
overrides, Gemini's system-settings path, OpenCode's inline config), from
files it owns under `<data_dir>/hooks/` and regenerates on every start,
without touching any repository or the CLI's own settings.

`hooks install` is for sessions started outside the backbone. It copies the
hook files into `<data_dir>/hooks/` and adds tagged entries to the CLI's
settings: `~/.claude/settings.json`, `~/.codex/hooks.json` or
`~/.gemini/settings.json` (with `--dir`, the project's `.claude/`,
`.codex/` or `.gemini/` file instead). Re-running is idempotent;
`uninstall` removes only the backbone's entries. Codex asks once to trust
hooks it has not seen: accept them with `/hooks` in a session. OpenCode
loads its plugin only through the launch wiring; there is no install.
The hooks prefer `$BACKBONE_STATE_DIR` (exported into every session the
backbone starts), so one global install serves any data directory. Restart
running sessions afterwards.
