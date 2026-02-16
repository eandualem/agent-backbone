# Agent Backbone — Usage Guide

The automation backbone replaces the old `webhook-receiver.py` with a Prefect-powered system that adds queuing, close-then-next delivery, offline recovery, and a monitoring dashboard.

```
GitHub webhook → Gateway (port 9877) → Prefect flows → tmux send-keys
                                        ↕
                                   Prefect Dashboard
                                   http://localhost:4200
```

**What's new vs the old webhook receiver:**

| Capability            | Old (webhook-receiver.py)       | New (backbone)                                 |
| --------------------- | ------------------------------- | ---------------------------------------------- |
| Notification delivery | Fire-and-forget                 | Tracked via Prefect flow runs                  |
| Offline agents        | Notification dropped            | Recovered by agent_monitor (60s poll)          |
| Issue closed          | Nothing                         | Close-then-next: delivers next queued issue    |
| Monitoring            | `tmux attach -t webhook` + logs | Prefect dashboard with flow run history        |
| Scheduling            | None                            | Prefect deployments (cron, interval)           |
| Queue                 | None                            | GitHub Issues = the queue (open `for:` issues) |

---

## First-Time Setup

### 1. Environment file

```bash
cd ~/ws/core/code/WF/agent-backbone
cp .env.example .env
```

Edit `.env`:

```bash
# Required — your GitHub PAT (needs repo scope for issue queries)
GITHUB_TOKEN=ghp_your_token_here

# These defaults are fine — only change if needed
GATEWAY_PORT=9877
GITHUB_OWNER=eandualem
GITHUB_REPO=orchestration
```

The `WEBHOOK_SECRET` is loaded from `~/.claude/services/.webhook-secret` automatically (same file the old receiver used). If you want to override it, set it in `.env`.

### 2. Install dependencies

```bash
cd ~/ws/core/code/WF/agent-backbone
uv sync
```

### 3. Verify Prefect is working

```bash
uv run prefect version
```

Prefect stores its data in `~/.prefect/prefect.db` (SQLite). This was already initialized on your machine — no Docker needed.

### 4. GitHub webhook config

If you're migrating from the old webhook receiver, **no changes needed** — same port (9877), same endpoint (`/webhook`), same signature verification. The gateway is a drop-in replacement.

If setting up fresh, see the [GitHub Webhook Configuration](#github-webhook-configuration) section below.

---

## Starting and Stopping

All commands use the `as` alias (`alias as='~/.claude/services/agent-services.sh'`).

### The Easy Way — Start Everything

```bash
as start-backbone     # Starts Prefect server + gateway
as start-tunnel       # Start ngrok (if not already running)
```

This creates two tmux sessions:

- `prefect` — Prefect server (dashboard at http://localhost:4200)
- `gateway` — HTTP gateway listening on port 9877

### Individual Control

```bash
# Prefect server only
as start-prefect
as stop-prefect

# Gateway only
as start-gateway
as stop-gateway

# Both
as start-backbone
as stop-backbone
```

### Check What's Running

```bash
as status
```

Output:

```
=== Services ===
  prefect    : running (tmux session, http://localhost:4200)
  gateway    : running (tmux session, port 9877, pid 12345)
  ngrok      : running (https://abc123.ngrok-free.app)

=== Named Entities ===
  feynman             : running
  ike                 : running
  leo                 : not running
  ada                 : not running

=== Coding Agents (2/10 running) ===
  platform-api                  : running
  arclio-assistant              : running
```

### Rollback to Old Webhook

If something goes wrong:

```bash
as stop-backbone       # Stop Prefect + gateway
as start-webhook       # Start the old webhook-receiver.py
```

The old `webhook-receiver.py` is untouched at `~/.claude/services/webhook-receiver.py`.

---

## Prefect Dashboard

The dashboard is the main reason to run the Prefect server. It gives you full visibility into what the backbone is doing.

### Accessing It

```
http://localhost:4200
```

Open this in your browser after `as start-prefect` or `as start-backbone`.

### What You'll See

**Flow Runs** (main view) — every webhook event that was processed:

| Column     | Meaning                                                   |
| ---------- | --------------------------------------------------------- |
| Flow       | `issue-dispatcher`, `issue-lifecycle`, or `agent-monitor` |
| State      | `Completed` (green), `Failed` (red), `Running` (blue)     |
| Duration   | How long the flow took                                    |
| Start Time | When the event was processed                              |

Click any flow run to see:

- **Task runs** inside it (e.g., `resolve_session`, `deliver_notification`)
- **Logs** from that specific execution
- **Parameters** that were passed in
- **Timeline** showing task execution order

**Flows** tab — lists all registered flows:

- `issue-dispatcher` — handles new issues and comments
- `issue-lifecycle` — close-then-next delivery
- `agent-monitor` — periodic offline recovery

**Deployments** tab — scheduled flows (Phase 1 uses direct invocation, so this will be empty until you set up scheduled deployments).

### Dashboard Tips

- **Filter by flow name:** Click the flow name in the sidebar to see only those runs
- **Filter by state:** Use the state filter to find failed runs
- **Auto-refresh:** The dashboard polls automatically, but you can click refresh for immediate updates
- **Logs are per-task:** Click into a flow run → click a task → see its specific logs

### When the Dashboard Is Empty

The Prefect server is optional for Phase 1. Flows are invoked directly by the gateway (no worker/deployment needed). The server provides observability only — if it's down, notifications still work. When you restart the server, it reads from `~/.prefect/prefect.db` and all historical flow runs reappear.

---

## How It Works

### Issue Dispatch (new issues + comments)

```
GitHub webhook (issue opened/labeled/commented)
  → Gateway validates signature, deduplicates
  → Gateway invokes issue_dispatcher flow
  → Flow parses for: labels, resolves tmux sessions
  → Delivers notification to each target session
```

Same behavior as the old webhook receiver, but now tracked as a Prefect flow run.

### Close-Then-Next (the new capability)

```
GitHub webhook (issue closed)
  → Gateway invokes on_issue_closed flow
  → Flow identifies which entity was the for: target
  → Queries GitHub API for remaining open issues with that for: label
  → Sorts: blocking priority first, then oldest (FIFO)
  → Delivers next issue notification to entity's tmux session
```

**Example:** Ike has 3 open issues (#10, #11, #12). He closes #10. The backbone:

1. Detects #10 was closed with `for:ike`
2. Queries GitHub: open issues with `for:ike` label → #11, #12
3. #11 is oldest → delivers to Ike's session:
   ```
   Next issue in your queue: #11 [task] "Update mcp-hub config" (from feynman).
   Review with: mcp__github__issue_read(method:"get", ...)
   ```

### Agent Monitor (offline recovery)

```
Every 60 seconds (when deployed as a scheduled flow):
  → Check all entity tmux sessions
  → For each online session: query GitHub for open for:{entity} issues
  → If pending issues exist, deliver the oldest one
```

This catches issues that arrived while an agent was offline. When you start an agent session, within 60 seconds the monitor delivers any pending work.

**Phase 1 note:** The agent monitor runs as a scheduled deployment. See [Scheduling Flows](#scheduling-flows) to set it up.

---

## Scheduling Flows

Phase 1 uses direct flow invocation for webhook events (gateway imports and calls flows — no Prefect worker needed). But the agent monitor needs to run periodically, which requires a Prefect deployment + worker.

### Option A: Run Monitor Manually (Quick Test)

```bash
cd ~/ws/core/code/WF/agent-backbone
uv run python -c "import asyncio; from flows.agent_monitor import monitor_agents; print(asyncio.run(monitor_agents()))"
```

This runs the monitor once and prints results.

### Option B: Deploy as Scheduled Flow

This sets up the agent monitor to run every 60 seconds automatically.

**Step 1 — Start a Prefect worker** (in a new tmux session):

```bash
# Create a work pool first (one-time)
cd ~/ws/core/code/WF/agent-backbone
uv run prefect work-pool create agent-pool --type process

# Start the worker
tmux new-session -d -s prefect-worker -c ~/ws/core/code/WF/agent-backbone \
  "uv run prefect worker start --pool agent-pool"
```

**Step 2 — Create the deployment:**

```bash
cd ~/ws/core/code/WF/agent-backbone
uv run prefect deploy flows/agent_monitor.py:monitor_agents \
  --name agent-monitor \
  --pool agent-pool \
  --interval 60
```

**Step 3 — Verify in dashboard:**

Go to http://localhost:4200 → Deployments tab. You should see `agent-monitor` with a 60-second interval. The worker will execute it automatically.

### Option C: Custom Scheduled Flow (Example)

Say you want a morning check that runs at 8 AM daily. Create `flows/morning_check.py`:

```python
from prefect import flow
from flows.agent_monitor import monitor_agents

@flow(name="morning-check")
async def morning_check():
    """Run agent monitor + any other morning tasks."""
    result = await monitor_agents()
    # Add more morning tasks here
    return result
```

Deploy it:

```bash
uv run prefect deploy flows/morning_check.py:morning_check \
  --name morning-check \
  --pool agent-pool \
  --cron "0 8 * * *"
```

### Scheduling Options

Prefect supports multiple schedule types:

```bash
# Every N seconds
--interval 60

# Cron expression
--cron "0 8 * * *"          # 8 AM daily
--cron "*/5 * * * *"        # Every 5 minutes
--cron "0 9-17 * * 1-5"     # Hourly during work hours, weekdays

# RRule (for complex recurrence)
--rrule "FREQ=DAILY;BYDAY=MO,WE,FR;BYHOUR=9"
```

### Pausing/Resuming Scheduled Flows

From the dashboard: Deployments → click deployment → toggle Active/Paused.

Or via CLI:

```bash
uv run prefect deployment pause agent-monitor/agent-monitor
uv run prefect deployment resume agent-monitor/agent-monitor
```

---

## What Notifications Look Like

**New issue:**

```
New issue targeting you: #5 [spec-gap] "Missing auth spec" (from ada, blocking). Review with: mcp__github__issue_read(method:"get", owner:"eandualem", repo:"orchestration", issue_number:5)
```

**New comment:**

```
New comment on issue #5 from eandualem: "Updated the spec with error handling." Review with: mcp__github__issue_read(method:"get_comments", owner:"eandualem", repo:"orchestration", issue_number:5)
```

**Next issue (close-then-next):**

```
Next issue in your queue: #11 [task] [blocking] "Update mcp-hub config" (from feynman). Review with: mcp__github__issue_read(method:"get", owner:"eandualem", repo:"orchestration", issue_number:11)
```

---

## GitHub Webhook Configuration

If setting up from scratch (not migrating from old webhook).

### 1. Create webhook secret

```bash
openssl rand -hex 32 > ~/.claude/services/.webhook-secret
chmod 600 ~/.claude/services/.webhook-secret
```

### 2. Start services

```bash
as start-backbone
as start-tunnel
as status          # Copy the ngrok URL
```

### 3. Configure in GitHub

Go to https://github.com/eandualem/orchestration/settings/hooks → Add webhook:

| Field        | Value                                                      |
| ------------ | ---------------------------------------------------------- |
| Payload URL  | `https://YOUR-NGROK-URL/webhook`                           |
| Content type | `application/json`                                         |
| Secret       | Contents of `~/.claude/services/.webhook-secret`           |
| Events       | Select individual → **Issues** and **Issue comments** only |

Save. Test with **Redeliver** on the ping event.

> **ngrok free tier:** URL changes on every restart. Update the webhook URL in GitHub after each `as start-tunnel`. Consider upgrading to a fixed domain or switching to Cloudflare Tunnel for permanence.

---

## Daily Workflow

### Morning

```bash
# Start backbone + tunnel
as start-backbone
as start-tunnel

# Start the agents you need
as start-agent ike
as start-agent feynman
as start-agent platform-api

# Check status
as status
```

Open http://localhost:4200 in a browser tab to watch flow activity.

### During the Day

- Agents receive notifications automatically via tmux
- Close-then-next delivers queued work as issues are resolved
- Agent monitor recovers missed notifications when sessions come online
- Dashboard shows all activity — check it when you want to see what's flowing

### End of Day

```bash
as stop-all          # Stop all agent sessions
as stop-backbone     # Stop Prefect + gateway
as stop-tunnel       # Stop ngrok
```

Or nuclear: `tmux kill-server`

---

## Troubleshooting

### Gateway won't start — port in use

```bash
lsof -i :9877
kill $(lsof -ti :9877)
as start-gateway
```

### Notifications not arriving

Check in order:

1. **Services running?**

   ```bash
   as status
   ```

2. **Gateway receiving requests?**

   ```bash
   tmux attach -t gateway    # Watch for incoming webhook logs
   ```

3. **GitHub delivering?**
   Check https://github.com/eandualem/orchestration/settings/hooks → Recent Deliveries

4. **Target agent running?**

   ```bash
   tmux ls
   ```

5. **Flow failed?**
   Check http://localhost:4200 → look for red (Failed) flow runs → click for error details

### Close-then-next not working

1. Verify the closed issue had `for:{entity}` labels
2. Check that `GITHUB_TOKEN` is set in `.env` (the lifecycle flow queries the GitHub API)
3. Look at the `issue-lifecycle` flow run in the Prefect dashboard for errors
4. Common cause: token doesn't have `repo` scope — needs read access to issues

### Prefect dashboard not loading

```bash
# Is the server running?
tmux attach -t prefect

# Restart it
as stop-prefect
as start-prefect

# Check if port 4200 is available
lsof -i :4200
```

The dashboard is at http://localhost:4200 — not HTTPS.

### Agent monitor not running

If you set up a scheduled deployment and it's not executing:

```bash
# Is the worker running?
tmux ls | grep prefect-worker

# Check deployment status
uv run prefect deployment ls

# Check work pool
uv run prefect work-pool ls
```

### Rollback

```bash
as stop-backbone
as start-webhook       # Old webhook-receiver.py (shows deprecation warning)
```

Same port, same endpoint — ngrok config doesn't need to change.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  GitHub (eandualem/orchestration)                             │
│  Issue created/closed/commented → webhook fires               │
└──────────────┬───────────────────────────────────────────────┘
               │ HTTPS POST
               ▼
┌──────────────────────────┐
│  ngrok tunnel            │
│  public URL → :9877      │
│  (tmux session: ngrok)   │
└──────────────┬───────────┘
               │ HTTP POST /webhook
               ▼
┌──────────────────────────────────────────────────────────────┐
│  Gateway (gateway/server.py)                                  │
│  - Validate HMAC signature                                    │
│  - Deduplicate delivery IDs                                   │
│  - Normalize event → IssueEvent                               │
│  - Route: closed → lifecycle flow, else → dispatcher flow     │
│  (tmux session: gateway)                                      │
└──────┬───────────────────────────┬───────────────────────────┘
       │                           │
       ▼                           ▼
┌──────────────────┐    ┌──────────────────────────┐
│ issue_dispatcher │    │ on_issue_closed          │
│ (Prefect flow)   │    │ (Prefect flow)           │
│                  │    │                          │
│ Parse labels     │    │ Query GitHub API         │
│ Resolve sessions │    │ Find next open issue     │
│ Deliver to tmux  │    │ Sort: blocking → oldest  │
└──────────────────┘    │ Deliver to tmux          │
                        └──────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  agent_monitor (Prefect scheduled flow, every 60s)            │
│  - List active tmux sessions                                  │
│  - For each online entity: query GitHub for open for: issues  │
│  - Deliver pending issues to newly-online agents              │
└──────────────────────────────────────────────────────────────┘

                        ┌──────────────────┐
                        │ Prefect Server   │
                        │ localhost:4200   │
                        │ SQLite backend   │
                        │ (~/.prefect/)    │
                        │                  │
                        │ Dashboard:       │
                        │ - Flow runs      │
                        │ - Task details   │
                        │ - Logs           │
                        │ - Deployments    │
                        └──────────────────┘
```

### File Layout

```
~/ws/core/code/WF/agent-backbone/
├── gateway/server.py          # HTTP intake — validates, normalizes, invokes flows
├── flows/
│   ├── issue_dispatcher.py    # New issue/comment → parse → route → deliver
│   ├── lifecycle.py           # Issue closed → find next → deliver
│   └── agent_monitor.py       # Periodic: recover offline agents
├── src/
│   ├── config.py              # Entity→session mapping, BackboneConfig
│   ├── models.py              # Pydantic: IssueEvent, ParsedLabels, etc.
│   ├── tmux.py                # Async tmux operations
│   ├── notifications.py       # Message formatting
│   └── github.py              # GitHub REST API client (httpx)
├── tests/                     # Full test suite
├── pyproject.toml             # uv + hatchling + Prefect 3.x
├── Makefile                   # make lint / format / test / check
├── prefect.yaml               # Deployment definitions
├── .env                       # Your secrets (not committed)
└── USAGE.md                   # This file
```

---

## Quick Reference

```bash
# Start everything
as start-backbone && as start-tunnel

# Stop everything
as stop-backbone && as stop-tunnel

# Check status
as status

# Prefect dashboard
open http://localhost:4200

# Run tests
cd ~/ws/core/code/WF/agent-backbone && uv run pytest

# Run agent monitor once (manual)
cd ~/ws/core/code/WF/agent-backbone
uv run python -c "import asyncio; from flows.agent_monitor import monitor_agents; print(asyncio.run(monitor_agents()))"

# Rollback to old webhook
as stop-backbone && as start-webhook
```

as start-agent leo
as start-agent feynman
as start-agent ike
as start-agent age
as start-agent ike
as start-agent feynman
