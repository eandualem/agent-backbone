# GitHub integration

GitHub Issues is the task ledger: durable, visible to humans, editable from
any client. The backbone turns issue activity into messages for agents and
keeps each agent working one issue at a time.

## Setup

```toml
[github]
repo = "acme/coordination"   # where for:<agent> issues live
mode = "poll"                # or "webhook"
```

```bash
# .env — either a token…
GITHUB_TOKEN=$(gh auth token)      # or a PAT with `repo` scope
# …or a GitHub App
GITHUB_APP_ID=12345
GITHUB_APP_PRIVATE_KEY_PATH=~/.config/agent-backbone/app.pem
```

Create the labels once in the coordination repo (GitHub creates unknown
labels on first use through the API, but not through the UI):
`for:<agent>` for each agent, `from:<agent>` for each agent, `task`, `bug`,
`question`, `spec-gap`, `optimization`, `blocking`.

### Event intake

| Mode | Needs | Latency | Notes |
|---|---|---|---|
| `poll` | token | ≤ 30 s | Coordination repo + every agent-owned repo. Checkpoint in `<data_dir>/github-poll.json`. Safe across restarts |
| `webhook` + `gh webhook forward` | token, `GITHUB_WEBHOOK_SECRET` | instant | `gh webhook forward --repo=acme/coordination --events=issues,issue_comment,pull_request --url=http://127.0.0.1:7120/webhooks/github --secret=$GITHUB_WEBHOOK_SECRET` |
| `webhook` + tunnel | token, secret, a stable hostname (named cloudflared tunnel, ngrok domain) | instant | Repository → Settings → Webhooks → `https://<host>/webhooks/github`, content type JSON, events: Issues, Issue comments, Pull requests |

Running both at once is fine; events are deduplicated by id.

## Routing rules

| Situation | Who is notified |
|---|---|
| Issue opened/labelled with `for:reviewer` | `reviewer` (unless `from:reviewer` — no self-notification) |
| Issue opened in `acme/app`, no `for:` labels, and `[agents.builder] repo = "acme/app"` | `builder` |
| Pull request opened in an owned repo | the owner (informational, no queue gate) |
| Comment on an issue | everyone in `from:` ∪ `for:` except the commenter and names in `routing.ignore_targets` |
| Issue closed | the closed issue's targets each get their **next** issue |
| All sub-issues of a parent closed | the parent's targets get an "unblocked" message |

### One issue at a time

For each agent, issue delivery is gated:

1. An issue already delivered to this agent is not delivered again
   (`already_delivered`).
2. A new issue is held (`awaiting_ack`) while a previously delivered issue in
   the agent's current open queue has not been acknowledged.
3. Delivery is claimed atomically, so the webhook path, the poller and the
   monitor cannot double-deliver.

**Acknowledgement** = the agent commented on the issue. Detected from:
- the hook action log (Claude Code running `gh issue comment 42 …` or a
  GitHub MCP comment tool),
- a comment whose body starts with `[from:reviewer]`,
- comments fetched from GitHub by the monitor.

An issue is delivered **once**. If the agent never acknowledges it, the
backbone does not re-send it — the agent's queue simply stays blocked on
that issue (`awaiting_ack`) until a comment appears. Watch `/api/deliveries`
or `/queue` on Telegram for agents stuck this way.

### Next-issue ordering

`blocking` issues first; then by type weight (`spec-gap` 100, `bug` 90,
`task` 50, `question` 20, `optimization` 10, none 0); then by number of
dependents; then oldest first. Tune under `[priority_scoring]`.

## What the agent receives

```
[via:github issue:42] New issue targeting you: acme/app#42 [bug] "Fix flaky auth test" (from planner, blocking). Link: https://github.com/acme/app/issues/42
[via:github issue:42] New comment on acme/app#42 "Fix flaky auth test" from planner: "Repro steps added." Link: https://github.com/acme/app/issues/42
[via:backbone] Next issue in your queue: acme/app#43 [task] "Add rate limiting" (from planner). Link: https://github.com/acme/app/issues/43
[via:backbone] Dependencies resolved for acme/app#40 [task] "Ship v2" (from planner). All sub-issues are now closed. Link: …
```

Only a summary and a link are delivered — never the issue body. The agent
reads the issue itself (`gh issue view 42`, or a GitHub MCP tool), which
keeps the backbone out of the business of relaying untrusted text at length.

## What an agent is expected to do

Put this in the agent's instructions (CLAUDE.md / AGENTS.md). It is the
entire protocol:

1. **When you receive `[via:github issue:N] New issue …`**, read the issue
   (`gh issue view N --comments`) and start working on it.
2. **Acknowledge early**: post a short comment as soon as you have taken it
   (`gh issue comment N --body "[from:reviewer] On it — plan: …"`). The
   `[from:reviewer]` prefix is optional when the backbone's hooks are
   installed, and harmless otherwise.
3. **Discuss on the issue**, not in the terminal: questions to the opener or
   other agents go as comments; they are routed automatically.
4. **When done, close it** (`gh issue close N --comment "…"`). The backbone
   delivers your next issue.
5. **To hand work to another agent**, open an issue with `for:<agent>` and
   `from:<you>` (`gh issue create --label for:builder --label from:reviewer …`,
   or `POST /api/issues`). Do not message the agent directly about it — the
   issue is the record.
6. **Blocked?** Comment with what you need and stop; do not close.

The backbone never reads issue bodies into prompts, so everything an agent
learns about a task it learns by reading GitHub with its own tools — which is
also where the audit trail lives.

## Multi-repository setups

- One coordination repo for `for:` issues plus per-project repos owned by
  agents is the common shape. Issues in an owned repo route to the owner
  without labels; the coordination repo handles cross-agent work.
- Two agents can own the same repository; both are notified of its issues.
- The `repo` query parameter on the issues API addresses any repository the
  token can reach.
