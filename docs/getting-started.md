# Getting started

Ten minutes from clone to two agents talking, on one machine, with nothing
but tmux and SQLite.

## 0. Requirements

- macOS or Linux, Python 3.11+, `tmux`
- [uv](https://docs.astral.sh/uv/) (or plain `pip`)
- At least one agent CLI on your `PATH`: `claude`, `codex`, `gemini`,
  `opencode`, `aider`. (`shell` works for trying things out.)

## 1. Install

```bash
git clone https://github.com/eandualem/agent-backbone && cd agent-backbone
uv sync
```

Every command below is `uv run backbone …`; `uv tool install .` gives you a
plain `backbone` on your PATH.

## 2. Create a configuration

```bash
uv run backbone init
```

This writes two files in the current directory:

- `backbone.toml` — everything structural (agents, integrations, tuning)
- `.env` — secrets, starting with a freshly generated `BACKBONE_API_KEY`

The backbone finds `backbone.toml` by walking up from wherever you run it,
or at `~/.config/agent-backbone/backbone.toml`, or via `BACKBONE_CONFIG=/path`.
Put it wherever you keep project-level config; it does not need to live
inside an agent's repository.

Open `backbone.toml` and describe your agents:

```toml
[agents.reviewer]
dir = "~/code/app"
runtime = "claude"

[agents.builder]
dir = "~/code/app"
runtime = "codex"
```

That is the whole required configuration.

## 3. Check the environment

```bash
uv run backbone doctor
```

`doctor` verifies tmux, every agent's directory and runtime binary, the API
key, and (if configured) GitHub and Telegram credentials. Fix anything marked
`✗`.

## 4. Install the Claude Code hooks (recommended)

```bash
uv run backbone hooks install claude          # global: ~/.claude/settings.json
# or per project:
uv run backbone hooks install claude --dir ~/code/app
```

With hooks, Claude Code tells the backbone when it is busy, idle, waiting for
plan approval or waiting for a permission answer. Without them the backbone
falls back to reading the terminal, which works but is less precise. Other
runtimes use terminal reading only for now.

## 5. Run the backbone

```bash
uv run backbone up            # foreground, Ctrl-C to stop
uv run backbone up --detach   # or inside a tmux session named "backbone"
```

`up` starts the HTTP/Socket.IO API on `127.0.0.1:7120`, the background jobs,
the Telegram bot (if a token is set) and the GitHub connector (if a repo and
token are set). Everything is one process. Data lives in
`~/.local/share/agent-backbone/` (`backbone.db`, `state/`, `pids/`).

## 6. Start an agent and talk to it

In another terminal:

```bash
uv run backbone agent start reviewer
uv run backbone status
uv run backbone tell reviewer "Summarise what this repository does in three sentences."
```

> **First launch of Claude Code in a new directory** shows its workspace-trust
> prompt, and its default answer is *No, exit*. Attach once with
> `tmux attach -t reviewer`, choose *Yes, I trust this folder*, detach with
> `Ctrl-b d`. Claude remembers the answer for that directory.

`tell` returns the delivery outcome:

```json
{"ok": true, "session": "reviewer", "outcome": "delivered"}
```

If the agent was busy you get `"outcome": "agent_working"` and the message is
queued; it is delivered automatically when the agent becomes idle (within a
minute — see [How it works](how-it-works.md#3-sending-a-message)).

Watch it happen: `tmux attach -t reviewer`.

## 7. Let agents talk to each other

An agent sends a message by calling the API. With `curl` available in the
agent's environment, this is a one-liner the agent can run itself:

```bash
curl -s -X POST http://127.0.0.1:7120/api/messages \
  -H "Authorization: Bearer $BACKBONE_API_KEY" -H "Content-Type: application/json" \
  -d '{"target_session":"builder","from_entity":"reviewer","message":"Auth tests pass; please rebase your branch."}'
```

Tell your agents about this in their instructions file (CLAUDE.md /
AGENTS.md): who the other agents are, and that `[via:backbone from:X]` at the
start of a message means X sent it.

## 8. Optional: GitHub Issues as the task list

```toml
[github]
repo = "acme/app"
mode = "poll"          # no public URL needed
```

```bash
echo 'GITHUB_TOKEN=ghp_…' >> .env    # or: GITHUB_TOKEN=$(gh auth token)
```

Restart `backbone up`. Now an issue labelled `for:reviewer` is delivered to
the reviewer agent within 30 seconds, comments are routed to the other
participants, and closing an issue hands the agent its next one. Details and
the agent-side protocol are in [GitHub integration](github.md).

## 9. Optional: Telegram

Create a bot with @BotFather, put `TELEGRAM_TOKEN=…` in `.env`, and add your
chat id to `allowed_chat_ids` (send `/identify` to the bot to learn it once you
have added a placeholder id). See [Telegram](telegram.md).

## Where things are

| What | Where |
|---|---|
| Config | `backbone.toml` (structural), `.env` (secrets) |
| Database | `<data_dir>/backbone.db` (SQLite) |
| Agent state from hooks | `<data_dir>/state/<agent>.json`, `<data_dir>/state/actions.jsonl` |
| Installed hook script | `<data_dir>/hooks/claude_hook.py` |
| Poll checkpoint | `<data_dir>/github-poll.json` |
| API docs | `http://127.0.0.1:7120/docs` while running |
