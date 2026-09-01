# How it works

This page follows real requests through the system. Read
[Concepts](concepts.md) first if the vocabulary is new.

## 1. Starting an agent

`backbone agent start [--dir D] [--name N] [--runtime R] [--watch REPO]`
(or `POST /api/agents/start`, or `/start name` on Telegram):

1. **Discover.** Resolve the directory (default: cwd), derive the name from
   the directory name, read `git remote get-url origin` for the repository.
   If the agent is already known, its recorded settings are reused and
   updated. A known name registered for a directory that no longer exists
   is a move — the record follows the project; if the old directory still
   exists, the new one is a different project sharing a folder name and is
   registered as `name-2`.
2. **Record.** Upsert the agent (and any `--watch` repositories) in the
   database. The running backbone publishes a new configuration snapshot;
   every job uses it from the next tick.
3. **Launch.** `tmux new-session -d -s <name> -c <dir>` running the runtime
   command (`claude --model …`), with `BACKBONE_RUNTIME`, `BACKBONE_AGENT`,
   `BACKBONE_STATE_DIR` and the agent's `env` exported into the session.
   For Claude Code the command also carries
   `--settings <data_dir>/hooks/claude-settings.json` — a backbone-owned
   settings file, regenerated at every start, that wires the state hooks
   without touching the repository or `~/.claude/settings.json`.
4. **Wait until ready** (up to `timing.start_timeout_seconds`, 60 s): a
   fresh hook-written `idle` state, or a visible empty prompt for runtimes
   without hooks. If the runtime is asking a question (Claude's folder-trust
   prompt), `start` returns `waiting_for_human` with the question shown
   instead of guessing.
5. Broadcast a fresh snapshot on Socket.IO `/sessions`.

The backbone keeps no process handle; tmux owns the session. If the
backbone restarts, sessions keep running and are rediscovered. Sessions
you start by hand appear in `backbone status` with `configured: false` and
can still receive messages.

Stopping (`agent stop`) is `tmux kill-session`. The backbone refuses to
stop its own session. A session that dies on its own is **reported** (to
the escalation target and Telegram, once) — never restarted.

When the backbone is down, the CLI does the same thing directly against the
database and tmux, and says so.

## 2. State: how the backbone knows what an agent is doing

```mermaid
flowchart TD
    A[tmux session exists?] -->|no| OFF[offline]
    A -->|yes| B[hook state fresh? < timing.stale_threshold_seconds]
    B -->|yes| C[hook state is authoritative]
    B -->|no| D[read the terminal through the runtime adapter]
    D -->|busy marker| BUSY[busy]
    D -->|permission prompt| WFH[waiting_for_human permission]
    D -->|empty prompt| IDLE[idle]
    D -->|inconclusive| E{stale hook state usable?}
    E -->|idle / busy / plan with file| F[stale hook state]
    E -->|no| UNK[unknown]
```

**Hook state** (`<data_dir>/state/<agent>.json`) is written first by the
backbone itself — `agent start` records `starting` the moment the tmux
session exists — and from then on by the shipped Claude Code hook:

| Claude Code event | State written |
|---|---|
| `SessionStart` | `idle` (Claude is at its prompt) |
| `UserPromptSubmit` | `busy` (captures `owner/name#N` or `#N` from the prompt as the current issue) |
| `Stop` | `idle` |
| `Notification` "needs your permission…" | `waiting_for_human` / `permission` |
| `PreToolUse ExitPlanMode` | `waiting_for_human` / `plan`, plan text saved to `<data_dir>/state/plans/<agent>.md` |
| `PreToolUse AskUserQuestion` | `waiting_for_human` / `question` |
| `PostToolUse` of either | `busy` |
| `SessionEnd` | `unknown` |

A fresh hook state is **authoritative**: Claude Code keeps its `❯` input
box on screen while working, so the terminal alone would say "idle" while
it is busy. The hook also appends `gh issue comment …` calls (with the
repository when `--repo` is given) to `<data_dir>/state/actions.jsonl`;
that is how acknowledgements are detected without a spoofable text tag.

**Terminal reading** is the fallback. Each runtime adapter knows its prompt
character, its status chrome (lines to ignore), its busy indicator
(Claude: `esc to interrupt`), its permission prompts (`Do you want to
proceed?`, the folder-trust question), and how to tell typed text from a
dim placeholder.

Every reading carries its evidence. `backbone agent inspect app` shows the
state, the delivery condition, the evidence lines, the terminal tail and
the recent deliveries — the first thing to look at when a delivery did not
happen.

## 3. Sending a message

`backbone tell`, `POST /api/messages`, Telegram `/tell`, GitHub events and
agent-to-agent calls all end in the same function, `safe_deliver`:

```mermaid
sequenceDiagram
    participant C as Caller
    participant B as Backbone
    participant Q as Queue (database)
    participant T as tmux session
    C->>B: message for "app"
    B->>B: delivery condition (state + terminal)
    alt ready / unknown
        B->>T: clear copy mode if needed; paste-buffer; Enter
        B->>T: re-read pane: submitted? still in the box?
        B-->>C: delivered
    else offline / waiting_for_human / agent_working / human_typing / settling
        B->>Q: enqueue (direct messages, comments, notices)
        B-->>C: agent_working (etc.)
        Note over Q: delivered by agent-monitor (60 s) or delivery-retry (5 min)
    end
    B->>B: record the delivery (kind, repo, outcome, preview)
```

- **Envelope.** The API adds `[via:backbone from:<from_entity>] `; Telegram
  `[via:telegram from:<name>]`; GitHub events `[via:github issue:N]`.
- **Paste, don't type.** Text goes in through tmux's paste buffer as one
  chunk, then Enter; the pane is re-read to confirm the text left the input
  box. If the runtime queued it for its next turn (Claude does), that
  counts as delivered.
- **Busy is never bypassed.** `priority: true` (and issues labelled
  `blocking`) only get through `human_typing` and `settling`.
- **Comments on the issue the agent is working on** are delivered even
  while it is busy or waiting; comments on other issues wait.
- **Queue hygiene.** Undelivered after `timing.queue_expiry_minutes` (30):
  expired. Leased by a crashed drain: released after 5 min.
- **Everything is recorded**, direct messages included:
  `GET /api/deliveries`, `backbone agent inspect`.

## 4. GitHub

### Intake

| Intake | When | How |
|---|---|---|
| `poll` | `GITHUB_TOKEN` (or App credentials) and no webhook secret | every `github.poll_interval_seconds`, list issues and comments updated since the last stored event in each tracked repository |
| `webhook` | `GITHUB_WEBHOOK_SECRET` is set | GitHub posts to `/webhooks/github`; signature verified; **one backfill poll at startup** catches what happened while the backbone was down |
| `off` | no credentials, or `github.intake = off` | — |

Tracked repositories = every repository any agent owns or watches. Both
paths produce the same event, which is stored in the `events` table
(deduplicated by delivery id) and then routed.

### An issue is opened

```mermaid
sequenceDiagram
    participant GH as GitHub
    participant B as Backbone
    participant A as app (owner)
    participant O as orch (watcher)
    GH->>B: acme/app#42 opened, labels: bug, from:planner
    B->>B: owners(acme/app) = [app]; watchers = [orch]
    B->>GH: app's open queue (queue scope)
    B->>B: gate: already delivered? older issue unacknowledged?
    B->>B: claim (acme/app, 42) for app — atomic
    B->>A: [via:github issue:42] New issue targeting you: acme/app#42 [bug] "…" (from planner). Link: …
    B->>O: [via:github issue:42] FYI: new issue acme/app#42 [bug] "…" (from planner). Link: …
```

Routing for an issue in repository R:

1. `for:<agent>` labels → those agents' queues (explicit always wins).
2. Otherwise, if R has one owner → its queue. Several owners → all of them
   get an "unassigned issue — comment to claim it" notice, nobody is queued.
3. Watchers of R get an informational notice (kind `watch`, never queued as
   work, expires like any queued message).
4. The sender (`from:`) never gets its own issue.

An **edit** of an existing issue (`labeled` events without a new `for:`
label) routes nobody.

The **issue queue gate** keeps issue delivery orderly: an agent gets one
issue at a time. A newer issue is held (`awaiting_ack`) while the last
delivered issue in the agent's open queue has not been acknowledged.
**Acknowledgement** = the agent commented on the issue: detected from the
hook action log, a `[from:<agent>]` comment prefix, or GitHub comments on
the next poll.

An agent's **queue** is the union of `for:<agent>` issues in every
repository it owns or watches plus, if it is the sole owner of its
repository, that repository's unlabelled open issues — ordered `blocking`
first, then type weight, then dependents, then age.

### A comment is added

Audience = `for:` targets ∪ the `from:` opener ∪ the sole owner, minus the
commenter and anyone in `routing.ignore_targets`. The commenter's
acknowledgement is recorded; the targets' acknowledgements are cleared.

### An issue is closed — close-then-next

1. Queued messages about the issue are purged.
2. Each target gets its **next** issue (highest priority in its queue).
3. The opener (`from:`), if it is an agent and not a target, is told the
   issue was closed.
4. If the issue was a sub-issue and all siblings are closed, the parent's
   targets get an "unblocked" notice.

### Pull requests

Opened in R: owners and watchers of R are told (informational).

## 5. Background monitoring

Every `timing.monitor_interval_seconds` (60 s), `agent-monitor`:

1. Refreshes agents and settings from the database.
2. Syncs sub-issue relationships for open issues.
3. **Stalls**: `busy` on one issue for > `timing.stall_threshold_seconds`
   (90 min) → one message to `escalation.target`, deduplicated for
   `timing.escalation_dedup_seconds` (30 min).
4. **Dead sessions**: an agent that had a live state and has no session now
   → reported to `escalation.target` and Telegram, once; state reset.
   Never restarted.
5. **Plan waiting**: Telegram notification (`/viewplan`, `/approve`) and a
   message to `escalation.target`, once per plan.
6. **Copy mode**: cancelled in every managed session; Telegram alert if it
   will not clear.
7. Socket.IO `/sessions` snapshot if anything changed.
8. Drains the queue for sessions that are now idle.
9. **Pending issues**: every idle agent with an empty in-flight slot gets
   the highest-priority unacknowledged open issue from its queue.

Every `timing.retry_interval_seconds` (5 min), `delivery-retry` re-attempts
issue deliveries that ended `offline`/`delivery_failed`/`agent_working`
and drains the queue again.

## 6. Integrations (Telegram)

Human-facing channels implement one contract ([Integrations](integrations.md)):
inbound text becomes a normal `safe_deliver` with a `[via:<integration>
from:<who>]` envelope, agents answer with `backbone reply` (→ `POST
/api/integrations/reply`), and alerts from the monitor go through
`notify_humans` into the agent's surface when it has one.

Telegram is the shipped integration. The bot runs inside the backbone
process and reads the live configuration. Commands map to the same
operations as the CLI (`/status`, `/tell`, `/start`, `/stop`, `/queue`,
`/digest`, `/viewplan`, `/approve`). In a forum group the bot creates one
topic per registered agent (on start, when agents change, every five
minutes): writing there talks to that agent, and the agent's `backbone
reply` lands there. See [Telegram](telegram.md).

## 7. Dashboards and scripts

- REST for state and actions (`/api/agents`, `/api/agents/{name}/inspect`,
  `/api/messages`, `/api/issues`, `/api/deliveries`, `/api/events`,
  `/api/plans`, `/api/status`, `/api/config`).
- Socket.IO `/sessions`: a full snapshot of all agents (`sessions:update`)
  whenever anything changes, and at least every minute.
- Socket.IO `/terminal`: a **read-only** live stream of any session's
  terminal output. Nothing typed in a browser reaches an agent.

See [API](api.md).

## 8. When the backbone is down

Agents keep running — they are tmux sessions. API calls fail. Messages
sent by other agents are lost (they should retry). GitHub events are
caught up on restart: poll intake resumes from the last stored event;
webhook intake runs its startup backfill. The `agent-monitor` job runs
its first tick immediately, so hook state for every running session is
re-read and missed plan-waiting notifications fire right after a restart.
