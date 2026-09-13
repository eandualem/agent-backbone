# Setup — install the backbone, run it, start the first agent, add connections

You are setting agent-backbone up for the person you work for. Do the steps
in order; each ends with a check. The only things you need a human for are
their accounts and secrets — every step says when.

Requirements: macOS or Linux, Python 3.11+, `tmux`, and at least one agent
CLI on the PATH (`claude`, `codex`, `opencode`, `deepcode`, `gemini`, …).

## 1. Install

```bash
uv tool install "agent-backbone[github-app]"     # or: pipx install "agent-backbone[github-app]"
backbone --help
```

If `backbone` is not found, run `uv tool update-shell` and use the full path
(`~/.local/bin/backbone`) until the human reloads their shell. `ab` is the
same command (macOS: `/usr/sbin/ab` may shadow it — use `backbone`).

## 2. Initialise and run

```bash
backbone init                 # data dir (~/.local/share/agent-backbone), .env with an API key, database
backbone service install      # runs now and at every login (launchd / systemd --user)
backbone doctor               # tmux, runtimes, credentials, API reachable
```

Check: `backbone status` shows `backbone API : up`. Without a login service,
`backbone up --detach` runs it inside a tmux session named `backbone`.

To restart after changing secrets: `backbone service install` again (it
replaces and restarts the service), or `backbone down && backbone up --detach`
when you run it by hand.

## 3. Start the first agent from its repository

```bash
cd <repository> && backbone agent start            # default runtime: claude
cd <repository> && backbone agent start --runtime codex --model gpt-5.2
```

The agent is named after the directory and owns the repository in
`git remote origin`. `start` returns when the agent is at its prompt.
`backbone runtimes` lists runtimes, whether each is installed, and example
model ids — pick from there; never ask the human for a model id.

Check:

```bash
backbone agent inspect <name>          # state: idle, delivery: ready, and the evidence
backbone tell <name> "Summarise this repository in three sentences."   # → "outcome": "delivered"
```

Folder-trust dialogs are answered for you (`agents.pre_trust`). What you
cannot do is grant an agent shell permissions: for Claude Code the human adds
an allowlist once to `~/.claude/settings.json` so agents can run the
lifecycle commands unattended — give them this snippet:

```json
"permissions": {
  "allow": [
    "Bash(backbone help)", "Bash(backbone help *)", "Bash(backbone docs *)",
    "Bash(backbone status)", "Bash(backbone agent list)",
    "Bash(backbone agent start)", "Bash(backbone agent start *)",
    "Bash(backbone agent stop *)", "Bash(backbone agent inspect *)",
    "Bash(backbone agent approve *)", "Bash(backbone agent watch *)",
    "Bash(backbone agent unwatch *)", "Bash(backbone tell *)", "Bash(backbone reply *)"
  ]
}
```

## 4. Connect GitHub (issues become the agents' task list)

```bash
gh auth token | backbone secrets set GITHUB_TOKEN    # piped: never a token in a command argument
```

If `gh` is not logged in, ask the human for a token with `repo` scope and
run `backbone secrets set GITHUB_TOKEN` without a value — it prompts, so
the token stays out of shell history and out of this conversation. Restart
(step 2). Check: `backbone status` shows `github intake: poll`.

That is poll intake, every 60 s, for every repository an agent owns or
watches. Webhooks (instant, plus every repository the human ever creates)
need a GitHub App and a tunnel — a human task with checkpoints:
`backbone docs github-app-setup`.

## 5. Connect Telegram (optional; the human creates the bot)

The human creates a bot with @BotFather and gives you the token and their
chat id. Then:

```bash
backbone secrets set TELEGRAM_TOKEN                   # prompts
backbone config set telegram.allowed_chat_ids '[<chat id>]'
```

Restart (step 2). In a group with Topics enabled, every agent gets its own
topic. Details: `backbone docs telegram`.

## 6. More agents, an orchestrator, swarms

Start each further agent from its own directory (step 3). An orchestrator is
an ordinary agent that watches the other repositories:

```bash
cd <orchestrator repository> && backbone agent start --watch acme/app --watch acme/web
```

Playbooks: `backbone help agents`, `backbone help messaging`,
`backbone help github`, `backbone help swarms`. Reference pages:
`backbone docs` (getting-started, concepts, how-it-works, configuration,
cli, api, github, telegram, security, …).

## 7. Report back

Tell the human what is running (`backbone status`), which agents you
started, and exactly which steps still need them: the permissions
allowlist (step 3), a GitHub token or App (step 4), a Telegram bot (step 5).
