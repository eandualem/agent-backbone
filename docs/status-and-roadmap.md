# Status and roadmap

Honest inventory of what works, what is missing, and what is next. Updated
2026-09-02.

## Works today (verified against live Claude Code sessions)

- Agents discovered from directories: name, runtime, repository from
  `git remote origin`; `agent start` returns when the agent is at its prompt
  and reports a folder-trust question instead of timing out.
- Runtime-agnostic states (`idle`, `busy`, `waiting_for_human` with reason,
  `starting`, `unknown`) from the runtime's hooks first and the terminal
  second, with evidence (`agent inspect`). Hooks ship for Claude Code,
  Codex, Gemini CLI and OpenCode, wired per launch without touching the
  CLI's own configuration; the wiring and the session-lifecycle events were
  verified live against codex-cli 0.152, Gemini CLI 0.46 and OpenCode 1.18
  (a permission dialog through each new hook is still to be captured live).
- State-gated delivery: a message sent while the agent is busy is queued
  and delivered exactly once when it is free; `priority` never interrupts a
  busy agent; copy mode is cleared automatically; every delivery (direct
  messages included) is recorded.
- Database-only configuration: settings with defaults, edited live with
  `backbone config`; secrets only in `.env`.
- GitHub per repository: owner / `for:` / `from:` / watch routing,
  one-issue-at-a-time with acknowledgement, close-then-next, sub-issue
  unblock, poll intake with the events table as checkpoint, webhook intake
  with startup backfill. Verified live end to end through a Cloudflare
  Tunnel with a GitHub App (app-level webhook, all repositories): open →
  deliver, comment → route, close → next, duplicates suppressed.
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
  under ten seconds; `make check` is the CI gate (GitHub Actions on 3.11–3.13).
- Packaging: published to PyPI by a manual workflow (never on a push or
  merge), which then tags `v<version>`; the wheel carries the documentation
  (`backbone docs`) and the agent playbooks (`backbone help`), so an agent
  can install and set the backbone up from the package alone.

## Missing on purpose (not yet built)

| Gap | Why it matters | Plan |
|---|---|---|
| Hooks for Deep Code / Aider | Those runtimes are read from the terminal only; permission prompts and busy markers are recognised, but the signal is weaker than a hook | Neither has hooks today; the terminal path stays |
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
