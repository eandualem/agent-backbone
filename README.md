# agent-backbone

**Your agents already work. Now let them work together.**

agent-backbone is a local control plane that connects the terminal agents you already run — Claude Code, Codex, OpenCode, Deep Code, Gemini CLI, Aider, or any CLI you can put in a tmux window — across CLIs, models and repositories. A Claude Code agent can hand work to a Codex agent, an OpenCode agent can ask a Claude agent a question, and any agent can put a mixed-runtime swarm on a single GitHub issue — all on one machine.

It uses what you already have. Your agents stay ordinary sessions with their own login, configuration and model access; the backbone calls no model and adds no subscription. It starts the session, tells the agent who it is and how to reach the others, and stays out of the way.

Underneath is one guarantee: **you can address a live terminal agent from outside it, at any moment, without corrupting what it is doing.** The backbone knows whether each agent is idle, busy or waiting for a person, and can show you the evidence. A message that cannot land safely is stored and delivered when the agent is ready. Every delivery is recorded with who sent it. Attach to any session whenever you want to watch, guide or take over.

Work organises around your repositories, not around the tool. An agent is a directory; the repository it owns is its task list; an orchestrator is an agent that watches several. When an issue benefits from parallel work, an agent can create a temporary swarm — a coordinator plus members on the runtimes and models you name, sharing one worktree and branch — and when the issue closes, the swarm is torn down and its committed branch remains.

Your repositories stay clean: nothing is committed, tracked or configured per repository. GitHub Issues carry coordination across repositories; Telegram topics let you talk to individual agents from your phone; a REST API and Socket.IO feed let you build your own view.

> **Status:** pre-release. The core — state detection, safe delivery, GitHub routing, swarms, Telegram, the API — is tested and has been exercised against live Claude Code, Codex, OpenCode and Deep Code sessions. [Status and roadmap](https://github.com/eandualem/agent-backbone/blob/main/docs/status-and-roadmap.md) says what is verified and what is not.

## Install

Requirements: macOS or Linux, Python 3.11+, `tmux`, [uv](https://docs.astral.sh/uv/) (or pipx), and at least one agent CLI on your PATH.

```bash
uv tool install "agent-backbone[github-app]"   # https://pypi.org/project/agent-backbone/
backbone init                                  # data dir, .env with an API key, database
backbone service install                       # runs now and at every login (launchd / systemd --user)
```

`pipx install "agent-backbone[github-app]"` works the same. If `backbone` is not found afterwards, run `uv tool update-shell` once and open a new terminal. `ab` is the same command under a short name (on macOS `/usr/sbin/ab`, Apache Bench, may shadow it — put `~/.local/bin` first in your PATH or use `backbone`).

No config file, no database server, no tunnel: everything the backbone knows lives in `~/.local/share/agent-backbone/` (a SQLite file, hook state, `.env`); settings are changed with `backbone config set`.

### Or let an agent do it

Paste this into any agent that has a shell — a Claude Code, Codex or OpenCode session in one of your repositories:

> Install agent-backbone from PyPI (`uv tool install "agent-backbone[github-app]"`), then run `backbone help setup` and follow it: get the backbone running, start an agent in this repository, and tell me what still needs me.

Everything the agent needs ships with the package: `backbone help` (the playbooks — `setup`, `agents`, `messaging`, `github`, `swarms`) and `backbone docs` (this documentation, page by page). Every agent the backbone starts is briefed with the same at launch, so agents can start other agents, subscribe to repositories, message each other and create swarms without a person in the loop.

## Ninety seconds to the first win

```bash
cd ~/code/app
backbone agent start                 # → app: ready — claude repo acme/app
backbone tell app "Read every file under src/ and list the modules."
backbone tell app "…and then tell me which one is the largest."   # while it is still working
```

The second `tell` returns `"outcome": "agent_working"`: the text was **not** typed into a working terminal. `backbone agent inspect app` shows why — the state, the evidence behind it, and the message waiting. When the agent reaches its prompt, the message lands as if you had been sitting there. That is the guarantee everything else is built on; [Getting started](https://github.com/eandualem/agent-backbone/blob/main/docs/getting-started.md) walks through it with real output.

## What it does

- **Runs agents from any directory.** `cd ~/code/my-app && backbone agent start` — the agent is named after the directory, its repository is read from `git remote origin`, the runtime and model you choose are remembered, and the command returns when the agent is at its prompt. Folder-trust dialogs are answered for you.
- **Knows the state of every agent** — `idle`, `busy`, `waiting_for_human` (plan approval, permission prompt, question), `starting`, `unknown` — from the runtime's own hooks where it has them and from the terminal otherwise, always with evidence: `backbone agent inspect reviewer`.
- **Delivers safely.** Text is pasted into an agent only when it is idle — not while it is working, waiting for a person, or while you are typing in that terminal. What cannot land now is queued in SQLite and delivered when the agent is free; `priority` may skip the typing and settle checks but never interrupts a working agent. The few deliberate exceptions (an `unknown` state is attempted, a comment on the agent's *current* issue reaches it at once, GitHub issue notifications are retried rather than stored) are in [How it works](https://github.com/eandualem/agent-backbone/blob/main/docs/how-it-works.md).
- **Lets agents unblock each other.** `backbone agent approve <name>` answers a runtime's permission dialog — only while it is on screen, only with its affirmative key, every approval audited — so a coordinator can keep a swarm moving without a person watching.
- **Coordinates through GitHub Issues, per repository.** An issue opened in an agent's repository is its work; `for:<agent>` labels address issues explicitly; comments go back to the opener; closing an issue hands the agent its next one. An orchestrator is just an agent that *watches* other repositories.
- **Swarms.** `backbone swarm create research --issue acme/app#42 --member 'scout*3@claude/sonnet' --member coder@codex` — a coordinator plus members, each on its own runtime and model, briefed for their roles, sharing one worktree and branch, torn down when the issue closes.
- **Talks to you on Telegram.** `/tell reviewer fix the flaky test`, `/status`, plan-approval alerts, and a forum topic per agent.
- **Feeds your dashboard.** REST plus a Socket.IO stream of agent state and read-only terminal output. It ships no UI.

## Runtimes

Any CLI that runs in a terminal can be an agent; how much the backbone can do for it depends on the runtime:

| Runtime | Unattended start | Brief at launch | State detection | Delivery | Approve |
|---|---|---|---|---|---|
| `claude` (Claude Code) | ✅ | ✅ system prompt | ✅ hooks + terminal | ✅ verified | ✅ |
| `codex` | ✅ | ✅ first prompt | ✅ terminal | ✅ verified | ✅ |
| `opencode` | ✅ (no trust dialog) | ✅ first prompt | ✅ terminal | ✅ verified | ✅ |
| `deepcode` (Deep Code, DeepSeek) | ✅ (no trust dialog) | ✅ `-p` | ✅ terminal | ✅ verified | pending |
| `gemini` | ✅ `--skip-trust` | ✅ first prompt | ✅ terminal | unverified¹ | — |
| `aider`, `shell` | — | first message | terminal, best effort | untested | — |

¹ Gemini CLI 0.46 completes Google OAuth and then refuses personal accounts ("no longer supported for Gemini Code Assist for individuals"); the backbone reports such a session as `waiting_for_human`. Delivery to a signed-in Gemini session (e.g. `GEMINI_API_KEY`) has not been tested yet. Deep Code is `@vegamo/deepcode-cli`, the community CLI DeepSeek's docs point to; its permission dialog has not been captured yet, so `agent approve` refuses it until then.

`backbone runtimes` lists every runtime, whether its binary is installed, and example model ids. The backbone deliberately does **not** manage per-repository runtime configuration (`CLAUDE.md`, `AGENTS.md`, MCP servers, …) — how a repository configures its tools is the repository's business.

## After the first win

**GitHub in two commands.** Issues become the agents' task list in every repository they own or watch:

```bash
gh auth token | backbone secrets set GITHUB_TOKEN   # the backbone's own .env, never a repo's
backbone service install                            # restart to pick it up
```

That is poll intake (every 60 s, nothing exposed). For instant delivery and automatic coverage of every repository you ever create, do the one-time GitHub App + webhook setup: [GitHub App setup](https://github.com/eandualem/agent-backbone/blob/main/docs/github-app-setup.md).

**An orchestrator** is an agent that watches the repositories it coordinates — `backbone agent start --watch acme/app --watch acme/web` — and opens issues for the others with `for:` and `from:` labels. **A swarm** puts parallel workers on one issue ([Swarms](https://github.com/eandualem/agent-backbone/blob/main/docs/swarms.md)). **Telegram** gives every agent a topic you can talk to from your phone ([Telegram](https://github.com/eandualem/agent-backbone/blob/main/docs/telegram.md)). **Your own view** builds on the [API](https://github.com/eandualem/agent-backbone/blob/main/docs/api.md).

## How it compares

Descriptions are each project's own, as of 2026-09-02; corrections welcome.

| | What it is | Where it is stronger | What the backbone adds |
|---|---|---|---|
| Claude Code Agent Teams | Teams of Claude sessions inside one Claude Code session | Zero setup if Claude Code is your only runtime | Other CLIs in the same team; agents addressable from outside the session; GitHub and Telegram; persistent agents that outlive a session |
| [claude-squad](https://github.com/smtg-ai/claude-squad) | A TUI to run several terminal agents (Claude Code, Codex, OpenCode, Amp) in tmux with worktrees | One-screen management, worktree-per-task ergonomics, diff review | Agents that talk to each other; state-gated, recorded delivery; issues as the task list; a phone |
| [vibe-kanban](https://github.com/BloopAI/vibe-kanban) | A kanban board over parallel coding-agent runs | A real UI, task board and review flow | No UI, but agent-to-agent messaging and persistent per-repository agents |
| [cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator) | Multi-agent orchestration for coding CLIs, coordinated in isolated tmux sessions | The closest structural peer; read both before choosing | Cross-CLI messaging with delivery gated on the recipient's live state; GitHub Issues as the routed work queue; Telegram; an agent-operated CLI |
| [agent-manager](https://github.com/YoanWai/agent-manager) | A tmux TUI: live status, quick prompts, worktrees, diff review | The single-screen view | Agent-to-agent messaging (which it lists as not yet supported), queues, GitHub, Telegram |

What none of them claims: a durable, state-gated address for a live terminal agent, usable from outside it — by another agent on a different CLI, by a GitHub issue, or by a phone.

## The security model, up front

The backbone types into your agents' terminals, so be clear about what it assumes:

- **One trusted user, one machine.** It runs as your OS user and drives tmux sessions that run as your OS user. There is **no isolation between agents**.
- **One key, full admin.** `BACKBONE_API_KEY` guards every authenticated route with the same weight. The CLI reads it from the data directory, so any session on the machine can use `backbone tell`; there is no scoped or read-only credential yet.
- **Agents do not receive the backbone's secrets.** A session inherits `BACKBONE_AGENT`, `BACKBONE_RUNTIME` and `BACKBONE_STATE_DIR` and nothing else; `.env` is kept out of agent environments. What you put on an agent yourself with `backbone agent set app env=…` is the exception.
- **Provenance is convention, not authentication.** `[via:backbone from:app]` says who *claims* to be speaking. An agent's instructions should treat text after an envelope as data, not orders.
- **Bound to `127.0.0.1` by default.** Put TLS and auth in front of it before exposing it.

Full detail: [Security](https://github.com/eandualem/agent-backbone/blob/main/docs/security.md).

## Where it came from

agent-backbone began as one component inside a larger, private orchestration system. That system's coupling turned out to be the problem — every part assumed every other part — so the backbone was extracted and rebuilt as a plug-and-play control plane with no dependency on any of it: it runs standalone, needs nothing but tmux and an agent CLI, and is the whole product rather than a piece of one. Issue references to `eandualem/orchestration` predate the split and point at a private repository.

It was built by the fleet it ships, with a human directing: the issues, reviews and commits in this repository are mostly agent-authored, through the backbone itself.

## Documentation

Also available from an installed package as `backbone docs <page>`.

| | |
|---|---|
| [Concepts](https://github.com/eandualem/agent-backbone/blob/main/docs/concepts.md) | The vocabulary: agent, repository, state, delivery, event |
| [Getting started](https://github.com/eandualem/agent-backbone/blob/main/docs/getting-started.md) | Install, start two agents, send the first message, add GitHub |
| [How it works](https://github.com/eandualem/agent-backbone/blob/main/docs/how-it-works.md) | Every flow step by step, with the decisions the backbone makes |
| [Configuration](https://github.com/eandualem/agent-backbone/blob/main/docs/configuration.md) | Settings (`backbone config`), secrets, the data directory |
| [CLI](https://github.com/eandualem/agent-backbone/blob/main/docs/cli.md) · [API](https://github.com/eandualem/agent-backbone/blob/main/docs/api.md) | Reference |
| [GitHub](https://github.com/eandualem/agent-backbone/blob/main/docs/github.md) · [App setup walkthrough](https://github.com/eandualem/agent-backbone/blob/main/docs/github-app-setup.md) · [Integrations](https://github.com/eandualem/agent-backbone/blob/main/docs/integrations.md) · [Telegram](https://github.com/eandualem/agent-backbone/blob/main/docs/telegram.md) | Integrations |
| [Swarms](https://github.com/eandualem/agent-backbone/blob/main/docs/swarms.md) | A coordinator plus members on one issue |
| [Security](https://github.com/eandualem/agent-backbone/blob/main/docs/security.md) | Defaults and what you opt into |
| [Status and roadmap](https://github.com/eandualem/agent-backbone/blob/main/docs/status-and-roadmap.md) | What works, what is missing, what is next |

## Development

```bash
git clone https://github.com/eandualem/agent-backbone && cd agent-backbone
make install     # uv sync --all-extras
make test        # pytest — SQLite in memory, no services required
make check       # lint + format check + tests
make dev         # backbone up --reload
```

`uv tool install --editable ".[github-app]"` gives you a global CLI that follows your checkout. See [CONTRIBUTING.md](https://github.com/eandualem/agent-backbone/blob/main/CONTRIBUTING.md).

## License

MIT — see [LICENSE](https://github.com/eandualem/agent-backbone/blob/main/LICENSE).
