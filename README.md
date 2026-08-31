# agent-backbone

A local control plane for terminal AI agents — Claude Code, Codex, Gemini CLI, OpenCode, Aider.

It starts your agents in tmux, delivers messages to them **only when they are ready to receive one**, lets them talk to each other and to you, and coordinates multi-agent work through GitHub Issues. Reach it from the CLI, from Telegram, or from any HTTP/Socket.IO client.

> **Status:** v2 is a ground-up rebuild and is pre-release. The core (delivery engine, state detection, GitHub routing, Telegram, API) is tested and has been exercised against live Claude Code sessions; packaging is still being finished.

## What it does

- **Runs agents from any directory.** `cd ~/code/my-app && backbone agent start` — the agent is named after the directory, its GitHub repository is read from `git remote origin`, and the command returns when the agent is at its prompt.
- **Knows the state of every agent** — `idle`, `busy`, `waiting_for_human` (plan approval, permission prompt, question), `starting`, `unknown` — from the runtime's own hooks first and the terminal second, and can show you the evidence: `backbone agent inspect reviewer`.
- **Delivers safely.** Text is pasted into an agent only when it is idle: never while it is working, never while it waits for a human, never while you are typing in that terminal. Everything else is queued durably and delivered when the agent is free.
- **Coordinates through GitHub Issues, per repository.** Every repository an agent lives in is tracked on its own. An issue opened in the agent's repository is its work; `for:<agent>` labels address issues explicitly; comments go back to the opener; closing an issue hands the agent its next one. An orchestrator is just an agent that *watches* other repositories.
- **Talks to you on Telegram.** `/tell reviewer fix the flaky test`, `/status`, plan-approval alerts, forum topics that map to agents.
- **Feeds dashboards.** REST plus a Socket.IO stream of agent state changes and (read-only) terminal output. It does not ship a UI.

## Quick start

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/) (or pip), `tmux`, and at least one agent CLI on your PATH.

```bash
git clone https://github.com/eandualem/agent-backbone && cd agent-backbone
uv sync                              # or: pip install -e .

uv run backbone init                 # data dir + .env (generated API key) + database
uv run backbone hooks install claude # Claude Code reports its state to the backbone
uv run backbone up --detach          # API + scheduler (+ Telegram/GitHub when configured)
```

Then, from a project:

```bash
cd ~/code/my-app
uv run backbone agent start          # → my-app: ready — claude repo me/my-app
uv run backbone tell my-app "summarise this repository in three sentences"
uv run backbone agent inspect my-app # state, delivery readiness, and why
uv run backbone status
```

No config file to edit, no database server, no tunnel. Everything the backbone knows lives in `~/.local/share/agent-backbone/` (a SQLite file, hook state, `.env`); settings are changed with `backbone config set`.

## The model in one paragraph

An **agent** is a directory plus a runtime, discovered the first time you start it. If the directory is a GitHub checkout, the agent **owns** that repository: unlabelled issues opened there are its work. Any agent can also **watch** other repositories (`backbone agent watch orch me/app me/web`), which makes `for:orch` labels in those repositories route to it and gets it an informational note about new issues. `from:<agent>` on an issue means replies come back to the opener. GitHub and Telegram are configured **once**, with a token in `.env`; nothing is configured per repository.

## GitHub in two commands

```bash
echo "GITHUB_TOKEN=$(gh auth token)" >> ~/.local/share/agent-backbone/.env
uv run backbone down && uv run backbone up --detach
```

That is **poll intake**: the backbone asks GitHub for new issues and comments every 60 s in every repository an agent owns or watches. For instant delivery, set `GITHUB_WEBHOOK_SECRET` and point a webhook (through `gh webhook forward` or a named cloudflared tunnel) at `/webhooks/github`; the backbone switches to **webhook intake** automatically and still runs one poll at startup to catch anything it missed while it was down. See [docs/github.md](docs/github.md).

## Documentation

| | |
|---|---|
| [Concepts](docs/concepts.md) | The vocabulary: agent, repository, state, delivery, event |
| [Getting started](docs/getting-started.md) | Install, start two agents, send the first message, add GitHub |
| [How it works](docs/how-it-works.md) | Every flow step by step, with the decisions the backbone makes |
| [Configuration](docs/configuration.md) | Settings (`backbone config`), secrets, the data directory |
| [CLI](docs/cli.md) · [API](docs/api.md) | Reference |
| [GitHub](docs/github.md) · [Telegram](docs/telegram.md) | Integrations |
| [Security](docs/security.md) | Defaults and what you opt into |
| [Status and roadmap](docs/status-and-roadmap.md) | What works, what is missing, what is next |

## Development

```bash
make install     # uv sync --all-extras
make test        # pytest — SQLite in memory, no services required
make check       # lint + format check + tests
make dev         # backbone up --reload
```

## License

MIT — see [LICENSE](LICENSE).
