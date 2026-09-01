# Getting started

Ten minutes from clone to two agents talking, on one machine, with nothing
but tmux and SQLite.

## 0. Requirements

- macOS or Linux, Python 3.11+, `tmux`
- [uv](https://docs.astral.sh/uv/) (or plain `pip`)
- At least one agent CLI on your `PATH`: `claude`, `codex`, `gemini`,
  `opencode`, `aider`. (`shell` works for trying the plumbing.)

## 1. Install the CLI

```bash
uv tool install "agent-backbone[github-app] @ git+https://github.com/eandualem/agent-backbone"
uv tool update-shell        # once, if ~/.local/bin isn't on your PATH yet
backbone --help
```

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

- `.env` — secrets only, starting with a generated `BACKBONE_API_KEY`
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

## 6. A second agent, and agents talking to each other

```bash
cd ~/code/web && backbone agent start --runtime codex
```

An agent sends a message by calling the API. With `curl` available, the
agent can run this itself:

```bash
curl -s -X POST http://127.0.0.1:7120/api/messages \
  -H "Authorization: Bearer $BACKBONE_API_KEY" -H "Content-Type: application/json" \
  -d '{"target_session":"web","from_entity":"app","message":"Auth tests pass; please rebase your branch."}'
```

Tell your agents about this in their instructions file (CLAUDE.md /
AGENTS.md): who the other agents are, and that `[via:backbone from:X]` at
the start of a message means X sent it. The API key is not exported into
agent sessions; give it to an agent deliberately:
`backbone agent set app env='{"BACKBONE_API_KEY":"…"}'`.

> **What that delegates**: there is one key and it is full-admin — an
> agent holding it can start, stop and reconfigure every agent, read every
> registered agent's terminal, and send messages under any name. Give it to
> agents whose instructions you control, and read `docs/security.md` first.

## 7. GitHub Issues as the task list

```bash
echo "GITHUB_TOKEN=$(gh auth token)" >> ~/.local/share/agent-backbone/.env
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

Create a bot with @BotFather, put `TELEGRAM_TOKEN=…` in `.env`, allow your
chat id (`backbone config set telegram.allowed_chat_ids '[123456789]'`),
restart. See [Telegram](telegram.md).

## After a reboot

Agents and the backbone are tmux sessions, so a reboot ends them (a
Cloudflare tunnel installed as a service comes back on its own):

```bash
backbone up --detach              # the backbone itself
backbone agent start app web      # the agents you want, by name
backbone status                   # confirm
```

There is deliberately no "start everything ever registered" — start the
agents you need, or keep a one-liner for the group you usually run
(e.g. `alias work-agents='ab agent start app web orch'`).

To start the backbone automatically at login on macOS, install a
LaunchAgent once (see [CLI → `up`](cli.md#backbone-up---detach---reload--backbone-down)).

## Where things are

| What | Where |
|---|---|
| Secrets | `<data_dir>/.env` |
| Settings, agents, events, deliveries | `<data_dir>/backbone.db` |
| Agent state from hooks | `<data_dir>/state/<agent>.json`, `<data_dir>/state/actions.jsonl` |
| Installed hook script | `<data_dir>/hooks/claude_hook.py` |
| API docs | `http://127.0.0.1:7120/docs` while running |
