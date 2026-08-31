# Telegram

A phone-sized control surface: see who is running, talk to an agent, approve
a plan, get told when an agent is stuck.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather); put the token in
   `.env` as `TELEGRAM_TOKEN`.
2. Find your chat id. Temporarily set `allowed_chat_ids = [0]` is *not*
   enough — the bot refuses to talk to anyone not on the list. Instead:
   message the bot, then read the id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates`, or add any id, start the
   backbone, and use `/identify` (it prints the chat id even outside a topic).
3. Configure:

```toml
[telegram]
allowed_chat_ids = [123456789]          # you (and optionally a group id)
notification_chat_id = 123456789        # where alerts go
```

4. Restart `backbone up`. The bot runs inside the backbone process; there is
   nothing else to start.

**The bot will not start with an empty `allowed_chat_ids`.** Every command
and message from an unlisted chat is ignored silently.

## Commands

| Command | Does |
|---|---|
| `/status` | Configured agents (🟢 running / ⚪ stopped) and other tmux sessions |
| `/tell <agent> <text>` | Deliver `[via:telegram from:<you>] <text>` through the normal readiness checks; replies with the outcome |
| `/start <agent>` / `/stop <agent>` | Start / stop a configured agent |
| `/queue` | Failed/pending and recent deliveries |
| `/digest` | Sessions, pending deliveries, tracked agent states |
| `/viewplan <agent>` | Show the plan an agent is waiting to have approved |
| `/approve <agent>` | Approve it — only when `[security] allow_remote_plan_control = true` |
| `/identify` | Print this chat/topic id and its current mapping |
| `/help` | Command list |

## Forum topics → agents

In a Telegram group with **Topics** enabled, each topic can be an agent's
inbox. Two ways to map them:

- **Automatic**: name the topic exactly like the agent (`reviewer`,
  `Builder`, `platform_api` → `platform-api`). The bot learns the mapping
  from the topic's creation message and stores it in
  `<data_dir>/telegram-topics.json`.
- **Explicit**: `[telegram.topic_routes]` with `thread_id = "agent"`
  (`/identify` inside the topic shows the id). Explicit wins.

Then writing `rebase onto main` in the *reviewer* topic is the same as
`/tell reviewer rebase onto main`. A topic mapped to `"agents"` is a
catch-all: `builder: run the tests` routes to `builder`.

Agents can answer into their topic:

```bash
curl -s -X POST http://127.0.0.1:7120/api/telegram/reply \
  -H "Authorization: Bearer $BACKBONE_API_KEY" -H "Content-Type: application/json" \
  -d '{"session": "reviewer", "text": "Done — PR #12 is green."}'
```

## Notifications you receive

Sent to `notification_chat_id`:

- **Plan waiting** — `📋 Plan waiting — reviewer / Title: … / /viewplan reviewer / /approve reviewer`, once per plan.
- **Copy mode stuck** — a session has been sitting in tmux copy mode and the
  automatic `q` did not clear it.

Stall and unexpected-offline escalations go to the `escalation.target`
agent, not to Telegram (see the open question in
[How it works → Background monitoring](how-it-works.md#5-background-monitoring)).
