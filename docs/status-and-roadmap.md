# Status and roadmap

Honest inventory of what works, what is missing, and what is next. Updated
2026-08-31.

## Works today (verified against live Claude Code sessions)

- Agents discovered from directories: name, runtime, repository from
  `git remote origin`; `agent start` returns when the agent is at its prompt
  and reports a folder-trust question instead of timing out.
- Runtime-agnostic states (`idle`, `busy`, `waiting_for_human` with reason,
  `starting`, `unknown`) from Claude Code hooks first and the terminal
  second, with evidence (`agent inspect`).
- State-gated delivery: a message sent while the agent is busy is queued
  and delivered exactly once when it is free; `priority` never interrupts a
  busy agent; copy mode is cleared automatically; every delivery (direct
  messages included) is recorded.
- Database-only configuration: settings with defaults, edited live with
  `backbone config`; secrets only in `.env`.
- GitHub per repository: owner / `for:` / `from:` / watch routing,
  one-issue-at-a-time with acknowledgement, close-then-next, sub-issue
  unblock, poll intake with the events table as checkpoint, webhook intake
  with startup backfill.
- Telegram: commands, forum-topic routing, plan-waiting and dead-session
  alerts, live configuration.
- REST API + Socket.IO snapshots + read-only terminal streaming; events
  feed; per-repository status.
- SQLite by default; Postgres optional; single Alembic migration.
- 734 unit tests; `make check` is the CI gate.

## Missing on purpose (not yet built)

| Gap | Why it matters | Plan |
|---|---|---|
| Hooks for Codex / Gemini / OpenCode / Aider | Those runtimes are read from the terminal only; permission prompts and busy markers are recognised, but the signal is weaker than a hook | Codex has a `notify` hook; Gemini CLI has hooks; OpenCode has plugins; Aider has none. Ship a hook adapter per runtime where one exists, keep the terminal path for the rest |
| GitHub App onboarding | With a token every agent comments as you; an App gives the backbone its own identity and one webhook for all repositories | Document the App setup; the client already supports installation tokens |
| Scheduled messages (`08:00 → tell app "daily triage"`) | Recurring nudges without a cron job | A `schedules` table → scheduler jobs that call `safe_deliver` |
| Swarms (N worker sessions in worktrees on one task) | "Orchestrate multiple agents on one job" | Thin layer over `agent start` with tags and a brief; no new state machine |
| Other trackers (GitLab, Linear) | GitHub-only today | Only if someone needs it; the GitHub client is the only tracker-specific code |
| Windows | tmux-only | Not planned |

## Known rough edges

- Claude Code's folder-trust prompt must be answered once per directory by
  a human (`start` tells you; `tmux attach`). The backbone will not
  auto-accept it.
- A queued message expires after 30 minutes (`timing.queue_expiry_minutes`).
  Comments that expire are still on GitHub; the agent finds them when it
  reads the issue.
- The `shell` runtime treats the `[via:…]` envelope as a glob. It exists for
  testing the plumbing, not for real use.
- The poll checkpoint is the newest stored event per repository; a repository
  with no events yet is scanned `github.backfill_lookback_hours` back on the
  first poll, so a fresh install may deliver day-old open issues. Close or
  label what you do not want delivered before adding the token.

## Where feedback is most useful right now

- Is one-issue-at-a-time per agent the right granularity, or should an
  agent be able to opt into N concurrent issues?
- Should acknowledgement be a comment (today) or a label the agent adds?
- Escalations go to one agent plus Telegram; is a per-agent escalation
  target needed?
- The GitHub App path: is a one-time App setup acceptable for an open-source
  user, or should the token path stay the primary one?
