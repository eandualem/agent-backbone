# GitHub integration

GitHub Issues is the task ledger: durable, visible to humans, editable from
any client. The backbone turns issue activity into messages for agents and
keeps each agent working one issue at a time — **per repository, with
nothing to configure per repository**.

## Setup — once

> **The complete step-by-step walkthrough with checkpoints and
> troubleshooting is [github-app-setup.md](github-app-setup.md).** Below is
> the short version.

Two decisions, independent of each other:

- **Intake** — how events reach the backbone: *poll* (zero setup, no public
  URL, ≤60 s latency) or *webhook* (instant, needs a stable public URL).
- **Auth** — who the backbone is on GitHub: a *token* (acts as you) or a
  *GitHub App* (acts as its own bot).

### Simplest: token + poll

```bash
gh auth token | backbone secrets set GITHUB_TOKEN   # → ~/.local/share/agent-backbone/.env
```

(Piped, so the token never appears in a process argument list. `.env`
holds plain values — the token itself, or a PAT with `repo` scope.)

Restart the backbone. `backbone status` now shows `github intake: poll` and
the tracked repositories: every repository an agent owns (its directory's
`origin`) or watches. Nothing is exposed and there is no URL to maintain —
this is the right starting point when you do not have a domain.

### Recommended: GitHub App + webhook — [full walkthrough](github-app-setup.md)

One-time setup (~15 min): a stable public URL (Cloudflare Tunnel with your
domain, or ngrok's free static domain for testing), a GitHub App holding
one app-level webhook, installed on **All repositories** — every repo you
ever create is covered automatically, and the backbone acts as its own bot
identity. In `.env`:

```bash
GITHUB_APP_ID=12345
GITHUB_APP_PRIVATE_KEY_PATH=~/.local/share/agent-backbone/github-app.pem
GITHUB_WEBHOOK_SECRET=<the app's webhook secret>
# remove GITHUB_TOKEN — a token takes precedence over the App
```

`backbone status` then shows `github intake: webhook`. The walkthrough has
a checkpoint after every step and the troubleshooting table (530 = wrong
hostname/path, 403 = secret mismatch, …).

### Token + webhook

Possible, but GitHub only attaches token-visible webhooks **per repository**
(personal accounts have no account-wide webhook) — Repo → Settings →
Webhooks → Add webhook with the same URL/secret/events for each repository.
Use the App instead unless you have a reason not to.

Labels are created on first use through the API; if you open issues from
the GitHub UI, create `for:<agent>`, `from:<agent>`, the types (`task`,
`bug`, `question`, `spec-gap`, `optimization`) and `blocking` once per
repository.

## Intake details

Setting `GITHUB_WEBHOOK_SECRET` switches intake from poll to webhook
(`github.intake` is `auto`). In webhook intake the backbone still runs
**one poll at startup** (`github.backfill_on_start`) to catch what happened
while it was down, and the monitor independently notices new open issues in
agents' queues. All paths produce the same event; the `events` table
deduplicates by delivery id and the per-issue delivery claim guarantees an
issue reaches an agent once, so overlap is safe. For a quick real-time test
without any tunnel, `gh webhook forward --repo=acme/app
--events=issues,issue_comment,pull_request,pull_request_review
--url=http://127.0.0.1:7120/webhooks/github --secret=$GITHUB_WEBHOOK_SECRET`
also works.

## Who hears about what

For an issue in repository R:

| Situation | Who is notified | How |
|---|---|---|
| `for:app` label (in any repository `app` owns or watches) | `app` | queued as work |
| No `for:` label, R has one owner | the owner | queued as work |
| No `for:` label, R has several owners | every owner | "Unassigned issue … comment to claim it" (not queued) |
| Any new issue in R | watchers of R | "FYI: new issue …" (not queued) |
| Comment | `for:` targets ∪ `from:` opener ∪ sole owner, minus the commenter and `routing.ignore_targets` | comment notice |
| Issue closed | each target gets its **next** issue; the `from:` opener is told it was closed | |
| All sub-issues of a parent closed | the parent's targets | "Dependencies resolved" |
| Pull request opened in R | owners and watchers of R, minus the agent that opened it | informational; the issues it closes count as acknowledged by the opener |
| Review submitted on a pull request | as for a comment, minus the reviewer when it is an agent | review notice: verdict, summary preview, link (one per review, not per inline comment; webhook intake only) |

The `from:` sender never receives its own issue. Editing an existing issue
(a `labeled` event without a new `for:`) notifies nobody.

### One issue at a time

For each agent, issue delivery is gated:

1. An issue already delivered to this agent is not delivered again.
2. A new issue is held (`awaiting_ack`) while a previously delivered issue
   in the agent's current open queue has not been acknowledged.
3. Delivery is claimed atomically per `(repository, number, session)`, so
   the webhook path, the poller and the monitor cannot double-deliver.

**Acknowledgement** = the agent commented on the issue. Detected from:
- the hook action log (Claude Code running `gh issue comment 42 …`, with
  the repository when `--repo` is given, or a GitHub MCP comment tool),
- a comment whose body starts with `[from:app]`,
- comments fetched from GitHub by the monitor.

An issue is delivered **once**. If the agent never acknowledges it, the
queue stays blocked on it (`awaiting_ack`) until a comment appears. Watch
`backbone agent inspect`, `/api/deliveries` or `/queue` on Telegram.

### The agent's queue and its order

`for:<agent>` issues in every repository the agent owns or watches, plus —
if it is the sole owner of its repository — that repository's unlabelled
open issues. "Unlabelled" means no `for:` label at all: an issue addressed
to a person (`routing.ignore_targets`) or to a name the backbone does not
know is not the owner's. Ordered `blocking` first; then type weight (`spec-gap` 100,
`bug` 90, `task` 50, `question` 20, `optimization` 10); then number of
dependents; then oldest first. Tune with `priority.*` settings.

## What the agent receives

```
[via:github issue:42] New issue targeting you: acme/app#42 [bug] "Fix flaky auth test" (from planner, blocking). Link: https://github.com/acme/app/issues/42
[via:github issue:42] New comment on acme/app#42 "Fix flaky auth test" from planner: "Repro steps added." Link: …
[via:github issue:7] FYI: new issue acme/web#7 [task] "Add rate limiting" (from planner for web). Link: …
[via:backbone] Next issue in your queue: acme/app#43 [task] "Add rate limiting" (from planner). Link: …
[via:github issue:12] Issue you opened was closed: acme/web#12 "…". Link: …
[via:backbone] Dependencies resolved for acme/app#40 [task] "Ship v2" (from planner). All sub-issues are now closed. Link: …
```

Only a summary and a link are delivered — never the issue body. The agent
reads the issue itself (`gh issue view 42 --repo acme/app`, or a GitHub
MCP tool).

## What an agent is expected to do

Put this in the agent's instructions (CLAUDE.md / AGENTS.md). It is the
entire protocol:

1. **When you receive `[via:github issue:N] New issue …`**, read it
   (`gh issue view N --repo owner/name --comments`) and start working on it.
2. **Acknowledge early**: comment as soon as you have taken it
   (`gh issue comment N --repo owner/name --body "[from:app] On it — plan: …"`).
3. **Discuss on the issue**, not in the terminal: questions to the opener or
   other agents go as comments; they are routed automatically.
4. **When done, close it** (`gh issue close N --repo owner/name --comment "…"`).
   The backbone delivers your next issue.
5. **To hand work to another agent**, open an issue in the repository the
   work belongs to with `for:<agent>` and `from:<you>`
   (`gh issue create --repo acme/web --label for:web --label from:app …`,
   or `POST /api/issues`). Do not message the agent directly about it — the
   issue is the record.
6. **Blocked?** Comment with what you need and stop; do not close.
7. **FYI notices** (`FYI: new issue …`, `Unassigned issue …`) are for your
   awareness; take an unassigned issue by commenting on it.

## Orchestrating several repositories

An orchestrator is an agent whose directory is its own repository (its
plans, notes, scripts) and which watches the repositories it coordinates:

```bash
cd ~/code/orchestration && backbone agent start --watch acme/app --watch acme/web
```

- It is told about every new issue in `acme/app` and `acme/web`.
- Anyone can address it with `for:orchestration` in those repositories.
- It opens issues for the others *in their repositories* with `for:app` /
  `for:web` and `from:orchestration`, and hears their comments and closes.
- Its own repository can hold its own unlabelled issues (it is the owner).

Two agents can own the same repository (two checkouts of one project):
unlabelled issues are announced to both and either claims one by
commenting; `for:` labels address one of them directly.
