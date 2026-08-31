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

## 3. Install the Claude Code hooks

```bash
backbone hooks install claude              # global: ~/.claude/settings.json
backbone hooks install claude --dir ~/code/app   # or one project
```

With hooks, Claude Code tells the backbone the moment it becomes busy, idle,
or waits for a person (plan approval, permission prompt, question). Without
them the backbone reads the terminal, which works but is less precise.
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

> **First launch in a new directory**: Claude Code asks whether you trust
> the folder. `start` reports `started, waiting for you` with the question
> shown; answer it with `tmux attach -t app` (choose *Yes, I trust this
> folder*, then `Ctrl-b d`). Claude remembers the answer for that directory
> and the next `start` is instant.

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

## 9. Optional: Telegram

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
