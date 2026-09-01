# Telegram

A phone-sized control surface: see who is running, talk to an agent,
approve a plan, get told when an agent is stuck or died.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather); put the token in
   `<data_dir>/.env` as `TELEGRAM_TOKEN`.
2. Find your chat id: message the bot, then read the id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Allow it and choose where alerts go:

```bash
backbone config set telegram.allowed_chat_ids '[123456789]'
backbone config set telegram.notification_chat_id 123456789
```

4. Restart `backbone up`. The bot runs inside the backbone process and reads
   the live configuration; there is nothing else to start.

**The bot will not start with an empty `telegram.allowed_chat_ids`.** Every
command and message from an unlisted chat is ignored silently.

## Commands

| Command | Does |
|---|---|
| `/status` | Known agents (🟢 running / ⚪ stopped) and other tmux sessions |
| `/tell <agent> <text>` | Deliver `[via:telegram from:<you>] <text>` through the normal readiness checks; replies with the outcome |
| `/start <agent>` / `/stop <agent>` | Start / stop a known agent |
| `/queue` | Failed/pending and recent deliveries |
| `/digest` | Sessions, pending deliveries, tracked agent states |
| `/viewplan <agent>` | Show the plan an agent is waiting to have approved |
| `/approve <agent>` | Approve it — only when `security.allow_remote_plan_control` is on |
| `/identify` | Print this chat/topic id and its current mapping |
| `/help` | Command list |

## Forum topics → agents

In a Telegram group with **Topics** enabled, each topic can be an agent's
inbox. Two ways to map them:

- **Automatic**: name the topic like the agent (`app`, `Web`,
  `platform_api` → `platform-api`). The bot learns the mapping from the
  topic's creation message and stores it in `<data_dir>/telegram-topics.json`.
- **Explicit**: `backbone config set telegram.topic_routes '{"42": "app"}'`
  (`/identify` inside the topic shows the id). Explicit wins.

Then writing `rebase onto main` in the *app* topic is the same as
`/tell app rebase onto main`. A topic mapped to `"agents"` is a catch-all:
`web: run the tests` routes to `web`.

Agents answer into their topic with `backbone reply "Done — PR #12 is
green."` (inside the agent session; the agent name comes from
`$BACKBONE_AGENT`), which is `POST /api/integrations/reply`:

```bash
curl -s -X POST http://127.0.0.1:7120/api/integrations/reply \
  -H "Authorization: Bearer $BACKBONE_API_KEY" -H "Content-Type: application/json" \
  -d '{"session": "app", "text": "Done — PR #12 is green."}'
```

Alerts about an agent (plan waiting, session died, copy mode stuck) are
posted into its topic too when it has one; otherwise they go to
`telegram.notification_chat_id`.

## Notifications you receive

Sent to `telegram.notification_chat_id`:

- **Plan waiting** — `📋 Plan waiting — app / Title: … / /viewplan app / /approve app`, once per plan.
- **Agent went offline unexpectedly** — a session died; it was not restarted.
- **Copy mode stuck** — a pane sits in tmux copy mode and the automatic
  cancel did not clear it.

Stall escalations go to the `escalation.target` agent.
