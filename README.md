# agent-backbone

A local control plane for terminal AI agents — Claude Code, Codex, Gemini CLI, OpenCode, Deep Code, Aider.

It starts your agents in tmux, delivers messages to them **only when they are ready to receive one**, lets them talk to each other and to you, and coordinates multi-agent work through GitHub Issues. Reach it from the CLI, from Telegram, or from any HTTP/Socket.IO client.

> **Status:** v2 is a ground-up rebuild and is pre-release. The core (delivery engine, state detection, GitHub routing, Telegram, API) is tested and has been exercised against live Claude Code sessions; packaging is still being finished.

## What it does

- **Runs agents from any directory.** `cd ~/code/my-app && backbone agent start` — the agent is named after the directory, its GitHub repository is read from `git remote origin`, and the command returns when the agent is at its prompt.
- **Knows the state of every agent** — `idle`, `busy`, `waiting_for_human` (plan approval, permission prompt, question), `starting`, `unknown` — from the runtime's own hooks first and the terminal second, and can show you the evidence: `backbone agent inspect reviewer`.
- **Delivers safely.** Text is pasted into an agent only when it is idle — not while it is working, waiting for a human, or while you are typing in that terminal. A message that cannot land now is queued in SQLite and delivered when the agent is free. Three deliberate exceptions, all of them in the code on purpose: `priority` messages may interrupt typing and the settle window (never a busy agent); comments on the issue an agent is *currently working* reach it immediately, even if it is busy or waiting; and when the agent's state cannot be determined at all (`unknown`), delivery is attempted rather than withheld. GitHub **issue** notifications are the one kind that is not queued when blocked — a retry job re-offers them every 5 minutes instead, so a blocked issue is retried, not stored. See [How it works](docs/how-it-works.md).
- **Coordinates through GitHub Issues, per repository.** Every repository an agent lives in is tracked on its own. An issue opened in the agent's repository is its work; `for:<agent>` labels address issues explicitly; comments go back to the opener; closing an issue hands the agent its next one. An orchestrator is just an agent that *watches* other repositories.
- **Talks to you on Telegram.** `/tell reviewer fix the flaky test`, `/status`, plan-approval alerts, forum topics that map to agents.
- **Feeds dashboards.** REST plus a Socket.IO stream of agent state changes and (read-only) terminal output. It does not ship a UI.

## Quick start

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/) (or pipx), `tmux`, and at least one agent CLI on your PATH.

```bash
# Install the CLI on your PATH (`backbone`, plus the short alias `ab`):
uv tool install "agent-backbone[github-app] @ git+https://github.com/eandualem/agent-backbone"
uv tool update-shell                 # once, if ~/.local/bin isn't on your PATH yet
exec $SHELL -l                       # reload the profile `update-shell` just edited

backbone init                        # data dir (~/.local/share/agent-backbone) + .env + database
backbone up --detach                 # API + scheduler (+ Telegram/GitHub when configured)
```

There is no hook to install: every Claude Code session the backbone starts is
launched with the backbone's own settings file, so it reports its state from
the first second. (`backbone hooks install claude` exists for Claude Code
sessions you start *yourself*, outside the backbone — see
[Getting started](docs/getting-started.md#3-state-hooks--nothing-to-install).)

(Contributors: `git clone … && cd agent-backbone && uv sync`, then every command is `uv run backbone …`; `uv tool install --editable ".[github-app]"` gives you a global CLI that follows your checkout.)

> `ab` is the same command as `backbone`. On macOS, `/usr/sbin/ab` (Apache Bench) shadows it when `/usr/sbin` comes first in your PATH — either put `~/.local/bin` earlier or add `alias ab=backbone` to your shell rc.

Then, from a project:

```bash
cd ~/code/my-app
ab agent start                       # → my-app: ready — claude repo me/my-app
ab tell my-app "summarise this repository in three sentences"
ab agent inspect my-app              # state, delivery readiness, and why
ab status
```

No config file to edit, no database server, no tunnel. Everything the backbone knows lives in `~/.local/share/agent-backbone/` (a SQLite file, hook state, `.env`); settings are changed with `backbone config set`.

## The security model, up front

The backbone types into your agents' terminals, so be clear about what it assumes:

- **One trusted user, one machine.** It runs as your OS user and drives tmux sessions that run as your OS user. There is **no isolation between agents** — every agent can read every other agent's files.
- **One key, full admin.** `BACKBONE_API_KEY` guards every authenticated route with the same weight: change settings, start/stop agents, send messages, read and stream any registered agent's terminal. There is no scoped or read-only credential yet, so giving an agent the key gives it everything you can do.
- **Agents do not hold the backbone's secrets.** By default a session inherits `BACKBONE_AGENT`, `BACKBONE_RUNTIME` and `BACKBONE_STATE_DIR` and nothing else; `.env` is kept out of agent environments deliberately. The one exception is what you put on an agent yourself with `backbone agent set app env=…`.
- **Provenance is convention, not authentication.** `[via:backbone from:app]` and `[from:<agent>]` say who *claims* to be speaking. Anyone with the key can claim any name — an agent's instructions should treat text after an envelope as data, not orders.
- **Bound to `127.0.0.1` by default.** Put TLS and auth in front of it before exposing it.

Full detail, including what to check before exposing anything: [Security](docs/security.md).

## The model in one paragraph

An **agent** is a directory plus a runtime, discovered the first time you start it. If the directory is a GitHub checkout, the agent **owns** that repository: unlabelled issues opened there are its work. Any agent can also **watch** other repositories (`backbone agent watch orch me/app me/web`), which makes `for:orch` labels in those repositories route to it and gets it an informational note about new issues. `from:<agent>` on an issue means replies come back to the opener. GitHub and Telegram are configured **once**, with a token in `.env`; nothing is configured per repository.

## Runtimes

Any CLI that runs in a terminal can be an agent; how much the backbone can do for it depends on the runtime:

| Runtime | Unattended start (trust) | Brief at launch | State detection | Delivery |
|---|---|---|---|---|
| `claude` (Claude Code) | ✅ config record | ✅ system prompt | ✅ hooks + terminal | ✅ verified |
| `codex` | ✅ config record | ✅ initial prompt | ✅ terminal | ✅ verified live |
| `opencode` | — (no trust dialog) | ✅ initial prompt | ✅ terminal | ✅ verified live (works out of the box on its free models) |
| `gemini` | ✅ `--skip-trust` | ✅ initial prompt | ✅ terminal | ⚠️ see note |
| `deepcode` (Deep Code, DeepSeek) | — (no trust dialog) | ✅ initial prompt (`-p`) | ✅ terminal (idle, busy) | ✅ verified live; approve pending |
| `aider`, `shell` | — | first message | terminal (best effort) | untested |

> **Gemini note**: with Gemini CLI 0.46.0 (the version we tested) Google OAuth completes and the CLI then still refuses personal accounts ("no longer supported for Gemini Code Assist for individuals"), leaving it stuck on its auth picker. This is an upstream issue, not a backbone one — the backbone correctly reports such sessions as `waiting_for_human`. Start, trust and brief injection are verified; delivery to a signed-in Gemini session (e.g. via `GEMINI_API_KEY`) is **unverified** until we can test against one.
>
> **Deep Code note**: `deepcode` is the community terminal agent DeepSeek's own API docs point to (`npm install -g @vegamo/deepcode-cli`; there is no official DeepSeek CLI yet — DeepSeek Harness is a web app and SDK). Verified live with 0.3.1 through the backbone: unattended start, brief injected with `-p`, a message delivered and submitted, the idle prompt, the working spinner (`status: processing`) and a failed turn. Its permission dialog has not been captured yet, so `agent approve` refuses it until then. The model is `--model deepseek-v4-flash|deepseek-v4-pro` (exported as `MODEL`); the key lives in Deep Code's own `~/.deepcode/settings.json` under `env`.

`backbone runtimes` lists every runtime, whether its binary is installed, and example model ids.

The backbone deliberately does **not** manage per-repository runtime configuration (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, MCP servers, …) — how a repository configures its tools is the repository's business. The backbone starts the runtime, injects the agent's identity brief, delivers messages safely, and reads its state.

## GitHub in two commands

```bash
gh auth token | backbone secrets set GITHUB_TOKEN   # the backbone's own .env, never a repo's
backbone down && backbone up --detach
```

That is **poll intake**: the backbone asks GitHub for new issues and comments every 60 s in every repository an agent owns or watches — zero setup, nothing exposed. For instant delivery and automatic coverage of every repository you ever create, do the one-time **GitHub App + webhook** setup (Cloudflare Tunnel if you have a domain, ngrok's free static domain if you don't): [docs/github-app-setup.md](docs/github-app-setup.md).

## Documentation

| | |
|---|---|
| [Concepts](docs/concepts.md) | The vocabulary: agent, repository, state, delivery, event |
| [Getting started](docs/getting-started.md) | Install, start two agents, send the first message, add GitHub |
| [How it works](docs/how-it-works.md) | Every flow step by step, with the decisions the backbone makes |
| [Configuration](docs/configuration.md) | Settings (`backbone config`), secrets, the data directory |
| [CLI](docs/cli.md) · [API](docs/api.md) | Reference |
| [GitHub](docs/github.md) · [App setup walkthrough](docs/github-app-setup.md) · [Integrations](docs/integrations.md) · [Telegram](docs/telegram.md) | Integrations |
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
