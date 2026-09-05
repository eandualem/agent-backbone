# Telegram

A phone-sized control surface: see who is running, talk to an agent,
approve a plan, get told when an agent is stuck or died.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and store its token:

   ```bash
   backbone secrets set TELEGRAM_TOKEN      # prompts; writes ~/.local/share/agent-backbone/.env
   ```

   That file is the **only** secrets file the backbone reads — never a
   `.env` in a project directory, because `agent start` runs inside your
   repositories and they have `.env` files of their own. `backbone secrets
   path` prints it; `backbone secrets list` shows what is set.
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
| `/approve <agent>` | Approve it — only when `security.allow_remote_plan_control` is on, and only for runtimes with a plan mode the backbone can drive (Claude Code) |
| *buttons on alerts* | A permission alert carries **Allow** / **Deny**, a plan alert **Approve plan** / **Reject plan** (see below). A button is bound to the prompt it was raised for: once the agent has moved on it answers nothing. Pressing one is answered once; the alert is edited with the outcome and who pressed it (name and Telegram user id), and a successful answer is recorded under the user id |
| `/identify` | Print this chat/topic id and its current mapping |
| `/help` | Command list |

## One topic per agent

The way to use the bot is a group with **Topics** enabled where **every
agent has its own topic** — writing in the *app* topic talks to `app`,
nothing to address. The bot creates and maintains those topics itself:

1. Create a group, enable Topics, add the bot and make it an administrator
   with **Manage Topics**. Add the group's chat id to
   `telegram.allowed_chat_ids`.
2. Send any message in the group (or `/identify`). The bot learns the
   group's id from it — or set it yourself:
   `backbone config set telegram.group_chat_id -1001234567890`.
3. Within a moment there is a topic per registered agent. New agents get a
   topic when they are registered (`backbone agent start` in a new
   directory); a forgotten agent's topic is **closed**, not deleted, and is
   reopened if the agent comes back under the same name. Swarm members
   (agents tagged `swarm:<name>`) never get one: a swarm is internal to the
   agent that runs it, and you talk to that agent.

In an agent's topic, plain text is delivered to that agent through the
normal readiness checks (`[via:telegram from:<you>] …`), and the bot
answers with the outcome (`Sent to app.` / `app is busy — queued.`). The
sender recorded for queueing is your stable Telegram user id
(`telegram:<id>`), so two people with the same first name never share a
queue identity; the envelope keeps the readable name. The agent replies
into the same topic with `backbone reply "…"`, and alerts
about it (plan waiting, session died) land there too.

The **General** topic is for the whole system: `/status`, `/start
<agent>`, `/tell <agent> <text>`, `/help`. Plain text there gets a pointer
to the topics rather than a guess at which agent you meant.

Mappings are stored in `<data_dir>/telegram-topics.json`. You can still
map a topic by hand — name a topic like the agent (`app`, `Web`,
`platform_api` → `platform-api`) and the bot learns it from the creation
message, or pin one with `backbone config set telegram.topic_routes
'{"42": "app"}'` (`/identify` shows the id; explicit wins and is never
closed automatically). A topic mapped to `"agents"` is the old catch-all
(`web: run the tests` routes to `web`). `backbone config set
telegram.auto_topics false` turns provisioning off if you prefer to manage
topics yourself.

Discovery binds to one group: the configured `telegram.group_chat_id`
when set, otherwise the first group that speaks. Messages from any other
allowed group teach no routes, and a topic thread from another group
never delivers into this group's agent. If you move the bot to a new
group, set `telegram.group_chat_id` to it — discoveries learned in the
old group are discarded and rediscovered (thread ids are per-group, and
threads learned before the bot tracked groups have no known origin), while
explicit `telegram.topic_routes` keep applying. The bot then re-learns and
re-provisions topics in the new group; old thread ids are never closed,
reopened or posted into there.

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

Posted into the agent's topic when it has one, otherwise to
`telegram.notification_chat_id`. Swarm members never get a topic: a swarm
is internal to the agent that runs it, and you talk to that agent.

- **Plan waiting** — `📋 Plan waiting — app / Title: … / /viewplan app / /approve app`, once per plan, with **Approve plan** / **Reject plan** buttons when `security.allow_remote_plan_control` is on.
- **Permission prompt** — `🔐 Permission prompt — app` followed by the dialog's own words (the command, the runtime's reason — runtime output, previewed), once per prompt, with **Allow** / **Deny** buttons when `security.allow_remote_approval` is on (the default). Allow sends the runtime's affirmative key, Deny its refusing key (Escape, verified for Claude Code and Codex; refused as unsupported elsewhere), only while the dialog is on screen, and every answer is recorded with who pressed it. Not sent while the tmux session is attached — someone is already looking at the dialog.
- **Question** — a dialog the backbone cannot answer for you (an `AskUserQuestion`, an unknown picker, or a *choice* such as Codex's rate-limit model switch, where Enter would pick rather than allow): the alert quotes it and says which terminal to attach to, without buttons.
- **Agent went offline unexpectedly** — an `always_on` agent's session died; it was not restarted.
- **Agent is offline with N queued messages** — messages are waiting for an agent that is not running (agents without `always_on`, which were not reported when they died; once per `timing.escalation_dedup_seconds`); it was not restarted.
- **Agent is blocked on its usage limit** — the runtime paused for its
  quota and will resume on its own (with what it said about the reset);
  once per `timing.escalation_dedup_seconds`.
- **Copy mode stuck** — a pane sits in tmux copy mode and the automatic
  cancel did not clear it.

Stall escalations go to the `escalation.target` agent.
