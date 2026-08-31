# GitHub integration

GitHub Issues is the task ledger: durable, visible to humans, editable from
any client. The backbone turns issue activity into messages for agents and
keeps each agent working one issue at a time — **per repository, with
nothing to configure per repository**.

## Setup — once

Two decisions, independent of each other:

- **Intake** — how events reach the backbone: *poll* (zero setup, no public
  URL, ≤60 s latency) or *webhook* (instant, needs a stable public URL).
- **Auth** — who the backbone is on GitHub: a *token* (acts as you) or a
  *GitHub App* (acts as its own bot).

### Simplest: token + poll

```bash
# <data_dir>/.env
GITHUB_TOKEN=$(gh auth token)      # or a PAT with `repo` scope
```

Restart the backbone. `backbone status` now shows `github intake: poll` and
the tracked repositories: every repository an agent owns (its directory's
`origin`) or watches. Nothing is exposed and there is no URL to maintain —
this is the right starting point when you do not have a domain.

### Recommended long-term: GitHub App + webhook through a Cloudflare Tunnel

One-time setup, then *every* repository on your account is covered forever —
no per-repository webhooks, and the backbone acts as its own bot identity.

**1. A stable public URL for the webhook** (any tunnel works; a named
[cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
tunnel on a domain you have on Cloudflare is free and permanent):

```bash
brew install cloudflared
cloudflared tunnel login                          # pick your zone
cloudflared tunnel create backbone
cloudflared tunnel route dns backbone hooks.example.com   # creates the DNS record
```

`~/.cloudflared/config.yml` — only the webhook path is exposed; the rest of
the API stays on 127.0.0.1:

```yaml
tunnel: <TUNNEL-ID>
credentials-file: /Users/you/.cloudflared/<TUNNEL-ID>.json
ingress:
  - hostname: hooks.example.com
    path: ^/webhooks/github$
    service: http://127.0.0.1:7120
  - service: http_status:404
```

Run it (`cloudflared tunnel run backbone`) and make it survive reboots with
`sudo cloudflared service install` — then copy the config where the daemon
looks and give the launchd job its run command (the installer misses both on
macOS):

```bash
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/config.yml ~/.cloudflared/<TUNNEL-ID>.json /etc/cloudflared/
sudo sed -i '' 's|/Users/you/.cloudflared/|/etc/cloudflared/|' /etc/cloudflared/config.yml
sudo /usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string tunnel" \
     -c "Add :ProgramArguments:2 string run" /Library/LaunchDaemons/com.cloudflare.cloudflared.plist
sudo launchctl kickstart -k system/com.cloudflare.cloudflared
cloudflared tunnel info backbone     # should list the daemon's connector
```

Sanity check: `curl -i -X POST https://hooks.example.com/webhooks/github`
returns 403 from the backbone (signature check), and `/health` returns 404
(path-filtered).

**2. The GitHub App** (Settings → Developer settings → GitHub Apps → New):

- Webhook: Active, URL `https://hooks.example.com/webhooks/github` — **the
  path matters**, a bare hostname gets a 530/404 — secret: `openssl rand -hex 32`.
- Repository permissions: *Issues: Read and write*, *Pull requests: Read and
  write*. Subscribe to events: *Issues*, *Issue comment*, *Pull request*.
- Generate a private key (downloads a `.pem`); note the **App ID**.
- **Install App** on your account → **All repositories** — this is what makes
  new repositories work with zero further setup. The backbone ignores events
  from repositories no agent owns or watches.

**3. Point the backbone at it:**

```bash
# <data_dir>/.env  (remove GITHUB_TOKEN — a token takes precedence over the App)
GITHUB_APP_ID=12345
GITHUB_APP_PRIVATE_KEY_PATH=~/.local/share/agent-backbone/github-app.pem
GITHUB_WEBHOOK_SECRET=<the same secret>
```

`chmod 600` the key, restart, and `backbone status` shows
`github intake: webhook`. Verify with the app's *Advanced → Recent
Deliveries* page: the ping and every event should show **200**. A 530 means
the URL's hostname is wrong; a 403 means the secret in the app form differs
from `.env` (both fixable under the app's Webhook settings, or via
`PATCH /app/hook/config`).

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
--events=issues,issue_comment,pull_request
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
| Pull request opened in R | owners and watchers of R | informational |

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
open issues. Ordered `blocking` first; then type weight (`spec-gap` 100,
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
