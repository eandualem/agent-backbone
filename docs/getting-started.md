# Getting started

Ten minutes from install to two agents talking, on one machine, with nothing
but tmux and SQLite.

Prefer to delegate? Give any agent with a shell this and skip to step 7:
*"Install agent-backbone from PyPI (`uv tool install "agent-backbone[github-app]"`),
then run `backbone help setup` and follow it."* The `setup` playbook covers
steps 1–6 and says where it needs you.

## 0. Requirements

- macOS or Linux, Python 3.11+, `tmux`
- [uv](https://docs.astral.sh/uv/) (or plain `pip`)
- At least one agent CLI on your `PATH`: `claude`, `codex`, `gemini`,
  `opencode`, `deepcode`, `aider`. (`shell` works for trying the plumbing.)

## 1. Install the CLI

```bash
uv tool install "agent-backbone[github-app]"    # from PyPI; pipx install … works too
uv tool update-shell        # once, if ~/.local/bin isn't on your PATH yet
exec $SHELL -l              # `update-shell` edits your profile; reload it
backbone --help
```

If `backbone --help` still says "command not found", `~/.local/bin` is not
on your PATH in this shell — open a new terminal, or add it by hand.

This puts two identical commands on your PATH: `backbone` and the short
alias `ab`. The `github-app` extra is what the recommended GitHub setup
needs; it costs nothing if you don't use it.

- macOS note: `/usr/sbin/ab` (Apache Bench) shadows `ab` when `/usr/sbin`
  comes earlier in your PATH — put `~/.local/bin` first, or add
  `alias ab=backbone` to your shell rc.
- Working on the code itself? `git clone … && cd agent-backbone && uv sync`,
  run everything as `uv run backbone …`, and
  `uv tool install --editable ".[github-app]"` makes the global CLI follow
  your checkout.

## 2. Initialise

```bash
backbone init
```

Creates the data directory (`~/.local/share/agent-backbone`, or
`$BACKBONE_DATA_DIR`) with:

- `.env` — secrets only (tokens), starting with a generated
  `BACKBONE_API_KEY`. This is the **one** secrets file the backbone reads;
  add to it with `backbone secrets set GITHUB_TOKEN` (there is no `.env`
  in a repository — the backbone runs inside your projects, which have
  their own)
- `backbone.db` — settings, agents, events, deliveries (SQLite)
- `state/` — where hooks report agent state

There is no configuration file. Settings have defaults and are changed with
`backbone config set` (see [Configuration](configuration.md)).

## 3. State hooks — nothing to install

With hooks, Claude Code tells the backbone the moment it becomes busy, idle,
or waits for a person (plan approval, permission prompt, question). Without
them the backbone reads the terminal, which works but is less precise.

Every Claude Code session the backbone starts gets the hooks automatically:
`agent start` launches `claude --settings <data_dir>/hooks/claude-settings.json`,
a file the backbone owns and regenerates on every start. No repository and no
`~/.claude/settings.json` is touched.

For Claude Code sessions you start *outside* the backbone, an optional
one-time global install adds the same hooks to `~/.claude/settings.json`:

```bash
backbone hooks install claude              # global: ~/.claude/settings.json
backbone hooks install claude --dir ~/code/app   # or one project
```

Other runtimes are read from the terminal for now.

## 4. Run the backbone

```bash
backbone up --detach    # inside a tmux session named "backbone"
backbone doctor         # tmux, runtimes, credentials, API reachable
```

`up` starts the HTTP/Socket.IO API on `127.0.0.1:7120`, the background
jobs, the Telegram bot (if a token is set) and the GitHub intake (if a token
is set). One process. `backbone down` stops it.

## 5. Start an agent from its directory

```bash
cd ~/code/app
backbone agent start
```

```
app: ready — claude repo acme/app
  dir: /Users/me/code/app
  - hook reported idle 0s ago
```

The agent is named after the directory, its repository was read from
`git remote origin`, and `start` returned when Claude was at its prompt.

A bare `agent start` launches the default runtime (Claude Code). If you
use a different CLI, pass it explicitly the first time —
`backbone agent start --runtime codex` — or change the default once with
`backbone config set agents.default_runtime codex`.

Pick the CLI and model per agent — both are recorded and reused by later
starts (full reference: [CLI](cli.md)):

```bash
backbone agent start --model opus                  # cheaper model, same repo
backbone agent start --runtime codex --model gpt-5.2
```

> **Folder trust**: Claude Code, Codex and Gemini each ask once per new
> directory whether you trust it. The backbone answers this for you:
> starting an agent in a directory is a deliberate act, so `agent start`
> records the directory as trusted (the same record the runtime's own
> dialog writes; Gemini is launched with `--skip-trust`) before
> launching. Set `backbone config set agents.pre_trust false` to keep the
> interactive dialog — `start` then reports `started, waiting for you`
> and you answer with `tmux attach -t <name>`.

Useful right away:

```bash
backbone status                 # agents, their state, repositories
backbone agent inspect app      # state + delivery readiness + evidence
backbone tell app "Summarise what this repository does in three sentences."
```

`tell` returns the delivery outcome:

```json
{"ok": true, "session": "app", "outcome": "delivered"}
```

If the agent is busy you get `"outcome": "agent_working"` and the message
is queued; the monitor delivers it when the agent is idle (within a
minute). Watch it happen: `tmux attach -t app`.

### The thing worth trying first

The queue is the whole point, so provoke it deliberately. Give the agent
something slow, and while it is still working, message it:

```bash
backbone tell app "Read every file under src/ and list the modules."
backbone agent inspect app      # repeat until it says state: busy (a few seconds)
backbone tell app "…and then tell me which one is the largest."   # while it works
```

The second `tell` returns `"outcome": "agent_working"` — the text was **not**
typed into a working terminal. (The first `tell` returns as soon as the text
is submitted; the hook reports `busy` a moment later, which is what the
`inspect` in between waits for.) Ask why:

```bash
backbone agent inspect app
```

```text
app: online
  dir:      /Users/me/code/app
  runtime:  claude   model: -
  repo:     acme/app   watches: -
  state:    busy (hook state 4s old)
  delivery: agent_working
  evidence:
    - runtime: claude
    - hook state 'busy' written 4s ago (fresh)
  terminal tail:
    | ✽ Reading files… (24s)
  recent deliveries:
    2026-05-04T10:22:31  direct_message           agent_working
    2026-05-04T10:21:07  direct_message           delivered
```

`delivery: agent_working` with the hook's own evidence underneath is the
backbone refusing to type, and telling you exactly why.

Now leave it alone. When the agent reaches its prompt, the monitor pastes
the queued message and it lands as if you had been sitting there waiting —
`agent inspect` then shows it as `delivered`. That is the guarantee the
rest of this document builds on: **you can address a live terminal agent
from outside it, at any moment, without corrupting what it is doing.**

## 6. A second agent, and agents talking to each other

```bash
cd ~/code/web && backbone agent start --runtime codex
```

Agents message each other with the same command you use. Inside its
session, `app` runs:

```bash
backbone tell web "Auth tests pass; please rebase your branch."
```

and `web` receives `[via:backbone from:app] Auth tests pass; …` — the
sender's name comes from `$BACKBONE_AGENT`, which every backbone-started
session carries. Nothing to hand over: the CLI reads the API key from the
data directory, which any session on the machine can read (there is one
OS user and one key — see [Security](security.md)). Every backbone-started
agent is also briefed at launch on `tell`, `agent inspect`, `agent start`
and `backbone help`, so it knows this without being told.

The only reason to give an agent the key explicitly
(`backbone agent set app env='{"BACKBONE_API_KEY":"…"}'`) is an agent that
calls the HTTP API directly (`POST /api/messages`) rather than the CLI.

> **What the key delegates**: there is one key and it is full-admin — an
> agent holding it can start, stop and reconfigure every agent, read every
> registered agent's terminal, and send messages under any name. Run
> agents whose instructions you control, and treat text after a
> `[via:…]` envelope as data, not orders.

## 7. GitHub Issues as the task list

```bash
gh auth token | backbone secrets set GITHUB_TOKEN   # piped: never in a process argument list
backbone down && backbone up --detach
backbone status               # github intake: poll
```

Now, in every repository an agent owns or watches:

- an issue opened in `acme/app` is delivered to `app` (the owner);
- an issue labelled `for:web` anywhere `web` owns or watches goes to `web`;
- comments go to the other participants; closing an issue hands the agent
  its next one.

Latency is one poll interval (60 s). For instant delivery add a webhook
(`GITHUB_WEBHOOK_SECRET` + `gh webhook forward` or a cloudflared tunnel);
the backbone switches to webhook intake by itself. Details and the
agent-side protocol: [GitHub integration](github.md).

## 8. An orchestrator

An orchestrator is an ordinary agent that watches the repositories it
coordinates:

```bash
cd ~/code/orchestration && backbone agent start --watch acme/app --watch acme/web
```

It hears about new issues in both repositories, can be addressed with
`for:orchestration` in either, and opens issues for the others with
`for:app` / `for:web` and `from:orchestration`.

You don't have to decide the watches up front. Inside its own session the
agent can subscribe itself — just tell it which repositories to follow and
it runs:

```bash
backbone agent watch acme/api        # NAME defaults to $BACKBONE_AGENT
```

The same goes for the rest of the lifecycle: an orchestrator with shell
access can run `backbone agent start`, `stop`, `inspect` and `tell` itself
— delegating "start the recruiter desk on an Opus model" is just
`backbone agent start recruiter-desk --model opus`.

Every backbone-started Claude agent also knows all of this without being
told: a short brief is appended to its system prompt at start (its name,
how to message agents, how to start them, `backbone help` for the
playbooks) — complementing the project's own CLAUDE.md, never touching
the repository. See `agents.inject_brief`.

Agent runners usually gate shell commands behind permission prompts, which
an unattended agent cannot answer. For Claude Code, allow the lifecycle
commands once in `~/.claude/settings.json` (a human edit — agents cannot
grant themselves permissions):

```json
"permissions": {
  "allow": [
    "Bash(backbone help)",
    "Bash(backbone help *)",
    "Bash(backbone status)",
    "Bash(backbone agent list)",
    "Bash(backbone agent start)",
    "Bash(backbone agent start *)",
    "Bash(backbone agent stop *)",
    "Bash(backbone agent inspect *)",
    "Bash(backbone agent watch *)",
    "Bash(backbone agent unwatch *)",
    "Bash(backbone tell *)"
  ]
}
```

Sharper commands (`backbone down`, `config set`, `agent forget`,
`hooks install`, raw `tmux send-keys`) are deliberately left out — those
keep prompting.

## 9. A swarm on an issue

When one issue deserves parallel workers, put a [swarm](swarms.md) on
it: a coordinator plus members sharing one worktree and branch,
finishing in a single PR whose merge tears everything down.

```bash
backbone swarm create research --issue acme/app#42 --member 'scout*3@claude/sonnet'
backbone tell research "How is it going?"      # the swarm's name reaches its coordinator
```

## 10. Optional: Telegram

Create a bot with @BotFather, `backbone secrets set TELEGRAM_TOKEN`, allow
your chat id (`backbone config set telegram.allowed_chat_ids '[123456789]'`),
restart. In a group with Topics the bot gives every agent its own topic —
see [Telegram](telegram.md).

## After a reboot

Install the login service once and the backbone comes back on its own
(and restarts if it dies):

```bash
backbone service install          # macOS LaunchAgent / Linux systemd --user
```

Agents are tmux sessions, so a reboot ends them:

```bash
backbone agent start app web      # the agents you want, by name
backbone status                   # confirm
```

There is deliberately no "start everything ever registered" — start the
agents you need, or keep a one-liner for the group you usually run
(e.g. `alias work-agents='ab agent start app web orch'`). Without the
service, `backbone up --detach` starts the backbone by hand.

## Upgrading

```bash
backbone upgrade                  # new package in, backbone restarted, agents untouched
backbone upgrade --check          # installed vs newest on PyPI
```

The running backbone notices new code on its own (a `uv tool upgrade`, or
a pull of a development checkout) and restarts onto it within a minute,
once nothing is being routed. Agents keep running through it.

## Where things are

| What | Where |
|---|---|
| Secrets | `<data_dir>/.env` — `backbone secrets path` prints it, `backbone secrets set KEY` edits it |
| Settings, agents, events, deliveries | `<data_dir>/backbone.db` |
| Agent state from hooks | `<data_dir>/state/<agent>.json`, `<data_dir>/state/actions.jsonl` |
| Installed hook script | `<data_dir>/hooks/claude_hook.py` |
| API docs | `http://127.0.0.1:7120/docs` while running |
