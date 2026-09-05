# Concepts

agent-backbone is a **control plane for terminal AI agents** on one machine.
You run Claude Code, Codex, Gemini CLI, OpenCode, Deep Code or Aider in tmux sessions;
the backbone starts those sessions, knows whether each one is ready to
receive input, delivers text to them safely, and connects them to GitHub
Issues, to people through **integrations** (Telegram today), and to each
other.

It is *not* an agent framework (it does not call models), not a workflow
engine (there are no DAGs), and not a dashboard (it feeds one).

## Agent

A directory plus a runtime. Agents are **discovered, not declared**: the
first `backbone agent start` from a directory records it.

```
$ cd ~/code/app && backbone agent start
app: ready — claude repo acme/app
```

- **Name** — the directory name (`--name` to override). It is the tmux
  session name, the value of `for:<name>` labels, and the `from:` identity
  in messages the agent sends.
- **Runtime** — `claude`, `codex`, `gemini`, `opencode`, `deepcode`, `aider`
  or `shell`; default `claude` (`agents.default_runtime`).
- **Repository** — read from `git remote origin`. An agent whose directory is
  a GitHub checkout **owns** that repository.
- **Watches** — other repositories the agent wants to hear about
  (`backbone agent watch orch acme/app acme/web`). Inside its own session
  an agent can subscribe itself: `backbone agent watch acme/app` (the name
  defaults to `$BACKBONE_AGENT`).

There are no roles, groups or hierarchies. An orchestrator is an ordinary
agent whose directory is its own repository and which watches the others.
The one grouping that exists is a [swarm](swarms.md): a coordinator plus
members sharing a worktree to complete a single issue — and its members
are still ordinary agents.

The database is the only record of agents; `backbone agent set|watch|forget`
edit it and the running backbone picks the change up immediately.

## Repository

Every repository an agent owns or watches is tracked **on its own**. There
is no coordination repository and nothing to configure per repository:
GitHub credentials are set once, and the backbone polls (or accepts
webhooks for) every tracked repository.

Four relationships decide routing for an issue in repository R:

| Relationship | How it comes about | Effect |
|---|---|---|
| **owner** | the agent's directory is R (swarm members excepted: their worktree is R, but they are not owners) | unlabelled issues are its work (sole owner) or are announced to all owners |
| **`for:<agent>`** | a label on the issue | goes to that agent's queue |
| **`from:<agent>`** | a label on the issue | comments and the close are reported back to the opener |
| **watch** | `backbone agent watch` | informational notice about new issues; `for:` labels in R route to it |

## State

What an agent is doing, in a vocabulary shared by every runtime:

| State | Meaning |
|---|---|
| `starting` | Session exists; runtime not at its prompt yet |
| `idle` | At the prompt, nothing running |
| `busy` | Working on a prompt |
| `waiting_for_human` | Blocked on a person — `reason` is `plan` (plan approval), `permission` (tool permission prompt) or `question` (`AskUserQuestion`, or any dialog seen on the terminal) |
| `blocked` | Waiting on something that is not a person and resumes on its own — `reason` is `quota` (the runtime's usage limit; `detail` says what the runtime said, e.g. when it resets) |
| `unknown` | No trustworthy signal |
| `offline` | No tmux session (reported by the API; not a stored state) |

Where it comes from, in order: `agent start` writes `starting` when the
session is created (trusted for two minutes at most; the first hook write
replaces it, and a visible prompt clears it). **Hooks** the runtime itself
runs (Claude Code, Codex, Gemini CLI and OpenCode) write
`<data_dir>/state/<agent>.json` on every
transition; a fresh hook state is authoritative. When there is no hook state or it is
older than `timing.stale_threshold_seconds` (5 min), the backbone reads
the **terminal** through the runtime's module (prompt visible, busy
marker, permission prompt). Every reading keeps its **evidence**, shown by
`backbone agent inspect`.

## Delivery condition

Derived from the state plus the terminal, right before anything is pasted:

| Condition | Meaning | Deliverable? |
|---|---|---|
| `offline` | no session | no — queued |
| `waiting_for_human` | agent is asking a person something | no — queued |
| `agent_working` | starting, busy, or blocked on its usage limit | no — queued (never bypassed) |
| `human_typing` | someone typed text into the prompt | no — queued, unless `priority` |
| `settling` | hook reported idle less than `timing.grace_period_seconds` ago | not yet, unless `priority` |
| `ready` | idle, empty prompt | **yes** |
| `unknown` | no signal either way | yes, best effort |

tmux **copy mode** (someone scrolled the pane) is not a condition: the
backbone cancels it automatically before delivering and on every monitor
tick. If the cancel fails, the session is reported as `human_typing`
(a frozen pane swallows pastes) and the message is queued.

## Message

The only thing that ever enters an agent's terminal. Every message carries
a provenance envelope so the agent — and anyone reading the transcript —
knows where it came from:

```
[via:backbone from:elias] review PR 12 and summarise the risks
[via:github issue:42] New issue targeting you: acme/app#42 [bug] "Fix flaky auth test" (from planner, blocking). Link: https://…
[via:telegram from:alice] status?
```

## Delivery

One attempt to hand a message to a session, recorded with its **kind**
(`issue`, `comment`, `review`, `pull_request`, `direct_message`, `watch`,
`escalation`, `plan_response`), repository, outcome and a preview — direct
messages included. A `plan_response` (an answer typed into a plan prompt)
is the one kind that is **never queued**: it goes in only while the agent
is waiting for a plan decision, and is refused as `not_waiting` otherwise. What cannot be delivered now is **queued** in the database and
delivered by the background jobs — a message that waited at least two
minutes is delivered with `(queued N min ago)` (`N h` from two hours) after
its envelope, so a review or comment drained after a long busy stretch
does not read as current; queued messages expire after
`timing.queue_expiry_minutes` (30), and an expired message leaves a
delivery with outcome `expired` (kind, source and preview kept), so
`agent inspect` shows what never arrived. The sender is told whether a row
exists (`stored`), whether the same message from them was already
waiting (`already_queued`), or whether storing it failed (`failed`) —
"queued" is never claimed for a message that is not in the database.
The same message means the same source event (a comment or review id) or
the same sender with the same text; two senders with identical text are two
messages. Issue deliveries are additionally
**claimed** so concurrent jobs cannot deliver the same issue twice. Delivery
checks and pastes are serialized per session within the running server;
different sessions can proceed concurrently. A blocked queue drain retains its
leased row instead of inserting an age-stamped copy.

## Event

Every inbound GitHub event (webhook or poll) is stored before it is
routed, with what the backbone did about it. That table is the activity
feed (`GET /api/events`, `backbone status` shows the last event per
repository) and the dedup record used by overlapping polls. A separate durable
cursor per repository preserves an incomplete batch across restarts, independent
of event retention. Event deduplication does not supply per-recipient delivery
receipts: queue storage failures during GitHub fan-out remain a known limitation
([audit report](reviews/2026-09-05-audit-2.md#recommendations-ranked)).

## Settings

Everything tunable is a key in the database with a built-in default:
`backbone config list`. Secrets (API key, tokens) are **never** in the
database; they live in `<data_dir>/.env` or the environment, and are
kept out of the agent sessions the backbone starts.

## Jobs

Background loops inside the backbone process:

| Job | Every | Does |
|---|---|---|
| `agent-monitor` | `timing.monitor_interval_seconds` (60 s) | refresh agents/settings, read every agent's state once and mirror it to the database, stall and dead-session reports, plan-waiting alerts, copy-mode clear, queue drain, next pending issue to idle agents, Socket.IO snapshot |
| `delivery-retry` | `timing.retry_interval_seconds` (5 min) | retry failed issue deliveries, drain the queue |
| `github-poll` | `github.poll_interval_seconds` (60 s) | poll intake only |
| `github-backfill` | once at startup | webhook intake only: catch up on what happened while the backbone was down |
| `prune` | 6 h | delete old deliveries, events and completed queue bodies; rotate the hook action log |

## What the backbone does not decide

- **What agents do.** It hands an agent text; the agent's instructions
  (CLAUDE.md, AGENTS.md, …) decide how it reacts. [GitHub → What an agent is
  expected to do](github.md#what-an-agent-is-expected-to-do) is the whole
  protocol.
- **Who may talk to whom.** Any agent can message any agent through the API.
- **Whether to restart a dead agent.** It reports; it never restarts.
