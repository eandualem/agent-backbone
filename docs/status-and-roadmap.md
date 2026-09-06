# Status and roadmap

Honest inventory of what works, what is missing, and what is next. Updated
2026-09-06.

## Implemented and checked

- Agents discovered from directories: name, runtime, repository from
  `git remote origin`; `agent start` returns when the agent is at its prompt
  and reports a folder-trust question instead of timing out.
- Runtime-agnostic states (`idle`, `busy`, `waiting_for_human` with reason,
  `starting`, `blocked` with quota/provider reason, `unknown`, and `offline`)
  from the runtime's hooks first and the terminal second, with evidence (`agent inspect`). Hooks ship for Claude Code,
  Codex, Gemini CLI and OpenCode, wired per launch without touching the
  CLI's own configuration; the wiring and the session-lifecycle events were
  verified live against codex-cli 0.152, Gemini CLI 0.46 and OpenCode 1.18
  (a permission dialog through each new hook is still to be captured live).
- State-gated delivery: a message sent while the agent is busy is queued
  and retried when it is free, until expiry; `priority` never interrupts a
  busy or provider-blocked agent. Current-issue comments obey the same gate.
  Copy mode is cleared automatically, and delivery attempts are recorded.
  A crash after terminal submission but before receipt storage can repeat a
  message; see [delivery receipts](github.md#delivery-receipts-and-retries).
- Database-only configuration: settings with defaults, edited live with
  `backbone config`; secrets only in `.env`.
- GitHub per repository: owner / `for:` / `from:` / watch routing,
  one-issue-at-a-time with acknowledgement, close-then-next, sub-issue
  unblock, poll intake with a durable replay cursor, webhook intake
  with one startup backfill, and durable per-recipient notification receipts.
  Verified live end to end through a Cloudflare Tunnel with a GitHub App (app-level webhook, all repositories): open →
  deliver, comment → route, close → next, duplicates suppressed.
- Configured GitHub reviewers: commit-anchored started/finished notices,
  durable completion state and suppression of delayed starts.
- Telegram: commands, forum-topic routing, plan-waiting and dead-session
  alerts, live configuration.
- REST API + Socket.IO snapshots + read-only terminal streaming; events
  feed; per-repository status.
- SQLite by default; Postgres optional; single Alembic migration.
- Swarms: a coordinator plus members (per-member runtime and model) in one
  shared worktree and branch, initiated on an existing issue, with
  injected role briefs, automatic teardown when the issue closes, and
  `tell <swarm>` reaching the coordinator ([Swarms](swarms.md)).
- A unit suite that runs with no services (SQLite in memory, tmux mocked) in
  about 14 seconds in the latest local run: 1,542 passing tests, no warnings.
  `make check` is the CI gate (GitHub Actions on 3.11–3.13); `make smoke` checks
  real tmux independently. See the [sanity check](reviews/2026-09-06-sanity.md).
- Packaging: published to PyPI by a manual workflow (never on a push or
  merge), which then tags `v<version>`; the wheel carries the documentation
  (`backbone docs`) and the agent playbooks (`backbone help`), so an agent
  can install and set the backbone up from the package alone.

## Missing on purpose (not yet built)

| Gap | Why it matters | Plan |
|---|---|---|
| Hooks for Deep Code / Aider | The shipped adapters read those runtimes from the terminal; the signal is weaker than a hook | Add hooks if a supported lifecycle API becomes available |
| Scheduled messages (`08:00 → tell app "daily triage"`) | Recurring nudges without a cron job | A `schedules` table → scheduler jobs that call `safe_deliver` |
| Auto-registering per-repo webhooks for token users | Personal accounts have no account-wide webhook; today token+webhook means clicking per repository | On agent discovery, `POST /repos/{owner}/{repo}/hooks` when a token with `admin:repo_hook` is present (the App path already avoids this entirely) |
| Other trackers (GitLab, Linear) | GitHub-only today | Only if someone needs it; the GitHub client is the only tracker-specific code |
| Windows | tmux-only | Not planned |

## Known rough edges

- Claude Code's folder-trust prompt is answered by the backbone at
  `agent start` (`agents.pre_trust`, on by default). With it disabled, a
  human answers once per directory (`start` tells you; `tmux attach`).
- A queued message expires after 30 minutes (`timing.queue_expiry_minutes`)
  and leaves a delivery with outcome `expired`. Comments that expire are
  still on GitHub; the agent finds them when it reads the issue.
- The `shell` runtime treats the `[via:…]` envelope as a glob. It exists for
  testing the plumbing, not for real use.
- The poll checkpoint is stored separately from event receipts and advances
  only after a complete batch succeeds. A repository with no cursor yet is
  scanned `github.backfill_lookback_hours` back on the first poll, so a fresh
  install may deliver day-old open issues. Close or label what you do not want
  delivered before adding the token.

## Where feedback is most useful right now

- Is one-issue-at-a-time per agent the right granularity, or should an
  agent be able to opt into N concurrent issues?
- Should acknowledgement be a comment (today) or a label the agent adds?
- Escalations go to one agent plus Telegram; is a per-agent escalation
  target needed?
- The GitHub App path: is a one-time App setup acceptable for an open-source
  user, or should the token path stay the primary one?
