# Status and roadmap

Capabilities and limitations of this checkout. Documentation checked 2026-09-22;
the dated runtime observations below are historical tests, not a guarantee for
every later CLI version or account.

## Implemented and checked

- Agents discovered from directories: name, runtime, repository from
  `git remote origin`; `agent start` waits for readiness and reports prompts or timeouts
  that need attention.
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
  Copy mode is preserved and blocks delivery as `human_reading`; delivery
  attempts are recorded.
  A crash after terminal submission but before receipt storage can repeat a
  message; see [delivery receipts](github.md#delivery-receipts-and-retries).
- Database-backed settings with defaults, edited live with
  `backbone config`; secrets only in `.env`.
- Event subscriptions ([sources](sources.md)): agents subscribe to Gmail
  with Gmail search filters and a priority; normal events batch in the queue
  until the agent is ready, high events reach a working Claude Code or Codex
  agent through its hook context. Unit-tested end to end with the IMAP
  client mocked; live mailbox validation is still pending.
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
  a short local run.
  `make check` is the CI gate (GitHub Actions on 3.11–3.13); `make smoke` checks
  real tmux independently.
- Packaging: published to PyPI by a manual workflow (never on a push or
  merge), which then tags `v<version>`; the wheel carries the documentation
  (`backbone docs`) and the agent playbooks (`backbone help`), so an agent
  can install and set the backbone up from the package alone.

## Runtimes

These adapters are shipped. “Verified” records live checks performed during
development; use `backbone runtimes` to see which binaries are installed locally.

| Runtime | Unattended start | Brief at launch | State detection | Delivery | Approve |
|---|---|---|---|---|---|
| `claude` (Claude Code) | ✅ | ✅ system prompt | ✅ hooks + terminal | ✅ verified | ✅ |
| `codex` | ✅ | ✅ first message | ✅ hooks + terminal | ✅ verified | ✅ |
| `opencode` | ✅ (no trust dialog) | ✅ first prompt | ✅ hooks + terminal | ✅ verified | ✅ |
| `deepcode` (Deep Code, DeepSeek) | ✅ (no trust dialog) | ✅ `-p` | ✅ terminal | ✅ verified | pending |
| `gemini` | ✅ `--skip-trust` | ✅ first prompt | ✅ hooks + terminal | unverified¹ | — |
| `aider` | — | first message | terminal, best effort | untested | — |
| `shell` | — | none | terminal, best effort | — | — |

¹ In a test with Gemini CLI 0.46, Google OAuth completed but the tested personal account was refused ("no longer supported for Gemini Code Assist for individuals"); the backbone reports such a session as `waiting_for_human`. Delivery to a signed-in Gemini session (e.g. `GEMINI_API_KEY`) has not been tested yet. Deep Code is `@vegamo/deepcode-cli`, the community CLI DeepSeek's docs point to; its permission dialog has not been captured yet, so `agent approve` refuses it until then.

## Current limits

- Deep Code and Aider use terminal detection; no lifecycle hooks are shipped.
- Issue routing supports GitHub. Other trackers are not integrated.
- Token-based webhook setup is per repository; a GitHub App can cover all
  repositories in its installation.
- macOS and Linux are supported. Windows is not supported because sessions
  depend on tmux.

## Known rough edges

- Claude Code's folder-trust prompt is answered by the backbone at
  `agent start` (`agents.pre_trust`, on by default). With it disabled, a
  human answers once per directory (`start` tells you; `tmux attach`).
- An ordinary queued message expires after 30 minutes (`timing.queue_expiry_minutes`)
  and leaves a delivery with outcome `expired`. Active swarm messages,
  subscription events and inbox holds have separate retention rules. Comments that expire are
  still on GitHub; the agent finds them when it reads the issue.
- The `shell` runtime treats the `[via:…]` envelope as a glob. It exists for
  testing the plumbing, not for real use.
- The poll checkpoint is stored separately from event receipts and advances
  only after a complete batch succeeds. A repository with no cursor yet is
  scanned `github.backfill_lookback_hours` back on the first poll, so a fresh
  install may deliver day-old open issues. Close or label what you do not want
  delivered before adding the token.

See [message checkpoints](cli.md#cooperative-message-checkpoints) for safe mid-turn
coordination, acknowledgement and retention. Report reproducible problems through
[GitHub Issues](https://github.com/eandualem/agent-backbone/issues).
