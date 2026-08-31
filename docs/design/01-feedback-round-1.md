# Feedback round 1 — direction corrections (2026-08-31)

Status: **PROPOSED** — the owner's feedback on the first usage docs and the
resulting design changes. Items marked *decided* were stated by the owner;
items marked *proposed* are the responses awaiting confirmation.

## Purpose restated (decided)

Multiple agents, possibly in different environments, collaborating through
effective agent-to-agent communication and issue delivery. **No hierarchy in
the backbone**: whether an agent is an orchestrator is its own business; the
backbone only routes and delivers. Detecting an agent's true state precisely
is the highest priority.

## 1. Agents are discovered, not declared

*Decided:* a declared registry is friction. Starting an agent from any
directory must be trivial; a name is derived from the directory unless given.

*Proposed:*
- `backbone agent start [--name N] [--runtime R]` from any directory. Default
  name = directory basename (suffix on collision). Repo = `git remote origin`
  of the directory, auto-detected.
- The backbone records every agent it starts in its database (`agents`
  table: name, dir, runtime, model, repo, watched repos, first/last seen).
  This is the source of truth for `status`, `/api/agents`, `for:<name>`.
- `[agents.*]` in `backbone.toml` becomes an optional seed for reproducible
  setups; entries are merged into the database on startup.
- `backbone agent forget N` removes a remembered agent.

## 2. States

*Decided:* `plan_waiting` is Claude-specific; copy mode is a defect to clear
automatically, not a state; `idle_grace`/`idle_ready` is a timing detail.

*Proposed split:*

Agent state (universal): `offline`, `starting`, `idle`, `busy`,
`waiting_for_human` (with `reason`: `plan`, `permission`, `question`),
`unknown`.

Delivery conditions (why not right now): `human_typing`, `settling`
(configurable grace after becoming idle). Copy mode is cleared on sight (in
the monitor and before every delivery) and never reported as a state.

## 3. Detection

*Decided:* precise, confusion-free state detection is the most important
capability.

*Proposed:*
- Hooks for every runtime that offers them (Claude Code: done; Codex
  `notify`; OpenCode plugin events; Aider notifications command; Gemini —
  survey). Terminal reading only as fallback.
- Recorded real-screen fixtures per runtime version as the test corpus for
  the fallback.
- `backbone agent inspect NAME`: state plus evidence (`source`, age, matched
  marker), so detection is auditable.
- `start` returns when the agent is at its prompt (hook or prompt detection,
  with timeout); a dead session is reported and escalated once, never
  auto-restarted.

## 4. Timing is configuration

*Decided:* every threshold is configurable; start with current defaults
(stale 5 min, grace 5 s, queue expiry 30 min, stall 90 min) and tune from
experience. Never paste into a busy agent unless priority.

*Proposed:* a `[timing]` section collecting them.

## 5. Delivery history

*Decided:* every delivery, including direct messages, is recorded.

## 6. GitHub: no coordination repository

*Decided:* no coordination/orchestration repository. Every repository an
agent works on is tracked independently; issues live with the code.
Orchestrator agents may span several repositories.

*Proposed repo model:*
- Watched repositories are derived from agents: a coding agent's own repo
  (from its directory) plus any repos an orchestrator explicitly watches
  (`backbone agent watch NAME owner/repo`).
- Intake: **webhooks**, one endpoint for all repos via a GitHub App
  installed on the org/user (recommended) or per-repo webhooks.
  `backbone status` shows each watched repo and whether events have been
  received from it.
- Every inbound event is stored (`events` table) before routing; it doubles
  as the activity feed.
- Routing relationships per repository:
  - `for:<agent>` → that agent's queue, regardless of repo. An
    orchestrator's queue is the union of `for:` issues across its repos.
  - **owner** (agent whose directory is the repo) → unlabelled issues in that
    repo. Several owners: announce to all, first acknowledgement claims.
  - `from:<agent>` → replies (comments, close) come back to the opener.
  - **watch** → informational notification only, never queued.
- Handoff = open an issue in the target's repo with `for:target from:me`.
  Non-repo requests are direct messages.

## 7. Polling

*Decided (owner):* polling every 30 s looks unnecessary if webhooks work.

*Proposed:* GitHub does not retry failed webhook deliveries, so events during
backbone downtime are lost. Keep the poller only as a **backfill**: once on
startup (since the last stored event) and optionally on a slow interval
(default off). Open: remove entirely and accept the gap?

## 8. Open: multiple hosts

"Different environments" may mean different machines. v2 is single-host.
If multi-host is in scope: one backbone per host, issues as the cross-host
channel, a small peer API for direct messages. Awaiting the owner's answer.

---

# Round 2 — clarifications (2026-08-31)

*Decided by the owner:*
- "Multiple environments" means multiple runtimes (Claude, Codex, Gemini …)
  on **one machine**. Multi-host is out of scope.
- GitHub and Telegram are configured **once**; nothing is configured per
  repository. Information must flow from every repository the agents use.
- A TOML file next to a database is two sources of truth. The **database is
  the only source of truth**; a CLI edits it.
- An orchestrator produces configuration, documents and data of its own and
  therefore has its own directory/repository, while watching the repos it
  coordinates.

*Proposed, awaiting go:*
- **No `backbone.toml`.** The data directory is the configuration:
  `backbone.db` (agents, settings with built-in defaults, events,
  deliveries, state), `.env` (secrets only), `state/`, `hooks/`.
  `backbone config get|set`, `backbone agent set|watch|forget`. The only
  external knob is `BACKBONE_DATA_DIR`.
- **GitHub intake = auto**: one credential; a GitHub App (installed once on
  the user/org → one webhook URL for all repos) when a URL is configured,
  otherwise token + polling of every watched repo (zero-setup path). The
  poller also backfills webhook gaps.
- **Orchestrator = ordinary agent** whose home directory is its own repo
  (owned like any other) and which *watches* other repos. Its notes and
  data live in its home repo; the backbone tracks only the agent record,
  state, events and deliveries.
- Per-repository relationships: owner (home dir is the repo) → unlabelled
  issues; `for:<agent>` → queue, in any owned or watched repo;
  `from:<agent>` → replies; watch → informational only. No coordination
  repository. Two owners of one repo: announce to both, first
  acknowledgement claims.

*Implementation order proposed:* (1) config collapse into the database,
(2) discovery-based `agent start` that returns at prompt, (3) generic
states + `agent inspect` + hooks survey, (4) GitHub intake and routing.
