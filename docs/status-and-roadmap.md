# Status and roadmap

Honest inventory of what works, what is missing, and what is next. Updated
2026-08-31.

## Works today (verified against a live Claude Code session)

- Start/stop configured agents in tmux; unconfigured sessions are visible too.
- Readiness from Claude Code hooks (`busy`, `idle`, `plan_waiting`,
  `permission_waiting`), terminal reading as fallback for all runtimes.
- State-gated delivery with durable queue, retry, dedup and atomic claims;
  a message sent while the agent is busy is held and delivered exactly once.
- GitHub Issues: webhook and polling intake, `for:`/`from:` routing,
  repo-owner routing, comment fan-out, close-then-next, sub-issue unblock,
  one-issue-at-a-time with acknowledgement.
- Telegram: commands, forum-topic routing, plan-waiting alerts.
- REST API + Socket.IO snapshots + read-only terminal streaming.
- `backbone` CLI: `init`, `doctor`, `up`, `down`, `status`, `agent`, `tell`,
  `hooks`.
- SQLite by default; Postgres optional; single Alembic migration.
- 729 unit tests; `make check` is the CI gate.

## Missing on purpose (not yet built)

| Gap | Why it matters | Plan |
|---|---|---|
| Hooks for Codex / Gemini / OpenCode / Aider | Those runtimes are read from the terminal only, which cannot see permission prompts or plan mode | Add per-runtime hook adapters where the CLI supports hooks; otherwise improve pane heuristics with recorded fixtures |
| Scheduled messages (`08:00 → tell reviewer "daily triage"`) | Replaces the old morning/evening routines and heartbeats | `[schedules]` table in config → scheduler jobs that call `safe_deliver` |
| Swarms (N worker sessions in worktrees on one task) | "Orchestrate multiple agents on one job" | Thin layer: `backbone swarm create --repo --task --workers N`; tags + a brief; no new state machine |
| Direct-message history | Dashboards cannot see `tell` traffic; only issue deliveries are recorded | Record all deliveries with a `kind` |
| Other trackers (GitLab, Linear) | GitHub-only today | Only if someone needs it; the GitHub client is the only tracker-specific code |
| Per-agent GitHub identity | All agents comment as one token | Per-agent token in `env`; document |
| Windows | tmux-only | Not planned |

## Known rough edges

- Claude Code's workspace-trust prompt must be answered once per directory
  by a human (`tmux attach`). The backbone cannot and should not auto-accept.
- A queued message expires after 30 minutes; see the open question in
  [How it works](how-it-works.md#3-sending-a-message).
- Escalations go to one agent; there is no "escalate to Telegram" switch yet.
- `agent start` returns as soon as tmux is up, not when the CLI is at its
  prompt; a `tell` in the same second may be queued rather than delivered.
- The `shell` runtime chokes on the `[via:…]` envelope (zsh treats it as a
  glob). It exists for testing tmux plumbing, not for real use.

## Phases

1. **Cut** — done. Removed Prefect, the entity registry, the dashboard
   surface and all environment-specific code.
2. **Plug-and-play** — done except scheduled messages. Single config file,
   SQLite, CLI, hooks, polling.
3. **Harden and refine** — next. `safe_deliver` as an explicit decision
   table; runtime adapters as a plugin registry with recorded pane fixtures;
   swarm redesign; direct-message history.
4. **Release** — docs site, examples, CI, PyPI.

## Where feedback is most useful right now

The **Open questions** in [How it works](how-it-works.md): start/tell
racing, stale threshold, queue expiry, one-issue-at-a-time vs. concurrency,
what "acknowledged" should mean, and where escalations should go.
