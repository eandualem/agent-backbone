# Concepts

agent-backbone is a **control plane for terminal AI agents**. You run Claude
Code, Codex, Gemini CLI, OpenCode or Aider in tmux sessions; the backbone
starts and stops those sessions, knows whether each one is ready to receive
input, delivers text to them safely, and connects them to GitHub Issues,
Telegram, and each other.

It is *not* an agent framework (it does not call models), not a workflow
engine (there are no DAGs), and not a dashboard (it feeds one). Its whole
value is in a small number of well-defined objects.

## The vocabulary

### Agent

A named, configured terminal agent. Declared once in `backbone.toml`:

```toml
[agents.reviewer]
dir = "~/code/app"      # where the CLI runs
runtime = "claude"      # which CLI
model = "claude-opus-5" # optional
repo = "acme/app"       # optional: issues in this repo belong to this agent
tags = ["review"]       # optional, free-form
```

The name does three jobs at once: it is the **tmux session name**, the value
of the **`for:<name>` label** that routes GitHub issues to it, and the
**`from:` identity** in messages it sends. There are no roles, groups,
organizations or hierarchies — an agent is a flat entry with a name.

### Session

A running instance of an agent: a detached tmux session running the agent's
CLI in its directory. Sessions are started by the backbone (`backbone agent
start reviewer`, the API, or Telegram) or by you (`tmux new -s reviewer`). A
session the backbone did not start still shows up and can still receive
messages; it just is not "configured".

### Runtime

The adapter for one CLI. It knows how to launch the binary, how to recognise
the CLI's prompt on screen, how to paste and submit text so it becomes a
single turn, and (for Claude Code today) how to install hooks that report
state. Supported: `claude`, `codex`, `gemini`, `opencode`, `aider`, `cursor`,
`shell`.

### Readiness

The single most important idea. Before anything is pasted into a session,
the backbone derives one of these states, in this priority order:

| State | Meaning | Deliverable? |
|---|---|---|
| `offline` | No tmux session | no — queued |
| `plan_waiting` | Agent is waiting for a human to approve a plan | no |
| `permission_waiting` | Agent is waiting for a permission prompt answer | no |
| `agent_working` | The model is thinking / running tools | no — queued |
| `copy_mode` | tmux is in copy/scroll mode (someone is reading) | recovers first, then delivers |
| `user_interacting` | A human has typed something into the prompt | no, unless `priority` |
| `idle_grace` | Just became idle; short settle window | not yet |
| `idle_ready` | At the prompt, nothing typed | **yes** |
| `unknown` | No signal either way | yes (best effort) |

Where the signal comes from: **hooks first** (the agent's own CLI reports
`busy`, `idle`, `plan_waiting`, `permission_waiting` into
`<data_dir>/state/<agent>.json`), then **the terminal itself** (what the pane
shows) when hook state is missing or stale. See
[How it works → Readiness](how-it-works.md#2-readiness-how-the-backbone-knows-an-agent-is-free).

### Message

The only thing that ever enters an agent's terminal. Every message carries a
provenance envelope so the agent — and anyone reading the transcript — knows
where it came from:

```
[via:backbone from:elias] review PR 12 and summarise the risks
[via:github issue:42] New issue targeting you: acme/app#42 [bug] "Fix flaky auth test" (from planner, blocking). Link: https://…
[via:telegram from:alice] status?
```

Messages have a **kind** (`direct_message`, `issue`, `comment`,
`pull_request`), an optional **issue reference**, and a **priority** flag
that lets it through even when a human is typing.

### Delivery

One attempt to hand a message to a session, with a recorded outcome
(`delivered`, `agent_working`, `offline`, `awaiting_ack`, …). Deliveries that
cannot happen now are **queued durably** in SQLite and retried by the
background jobs. Issue deliveries are additionally **claimed** so two jobs
can never deliver the same issue twice.

### Channel

Where messages come from and go to:

- **CLI** — `backbone tell reviewer "…"`
- **HTTP API** — `POST /api/messages` (what agents use to talk to each other)
- **GitHub Issues** — webhook or polling; issues and comments become messages
- **Telegram** — commands and forum topics mapped to agents

### Task ledger

GitHub Issues is the shared, durable, human-readable task list. An issue is
addressed to an agent with a `for:<agent>` label; the agent acknowledges by
commenting and finishes by closing; the backbone then delivers the next issue
in that agent's queue. Nothing about a task lives only in the backbone's
database — you can always see and change the state on GitHub.

### Jobs

Background loops that run inside the backbone process (no external
scheduler):

| Job | Every | Does |
|---|---|---|
| `agent-monitor` | 60 s | stall / offline / plan-waiting detection, copy-mode recovery, queue drain, deliver the next pending issue to idle agents, push live snapshots to Socket.IO |
| `delivery-retry` | 5 min | retry failed issue deliveries, drain queued messages |
| `github-poll` | 30 s | only in polling mode: fetch new issues/comments |
| `prune` | 6 h | delete old delivery rows |

## What the backbone does not decide

- **What agents do.** The backbone hands an agent text; the agent's own
  instructions (CLAUDE.md, AGENTS.md, …) decide how it reacts. The
  [GitHub page](github.md#what-an-agent-is-expected-to-do) describes the small
  protocol agents should follow (acknowledge, close).
- **Who is allowed to talk to whom.** Any agent can message any agent through
  the API. If you need policy, put it in the agents' instructions or in front
  of the API.
- **How work is decomposed.** Multi-agent coordination happens through issues
  and comments that humans can read; there is no planner inside the backbone.
