# HTTP & Socket.IO API

Base URL `http://127.0.0.1:7120`. Interactive OpenAPI docs at `/docs` while
running. Every route except `GET /health` and the webhook requires
`Authorization: Bearer <BACKBONE_API_KEY>`.

## Agents

### `GET /api/agents`

All configured agents plus any other live tmux session (except the
backbone's own), with live state. Cached for 5 s.

```json
{"items": [{
  "name": "reviewer", "session": "reviewer", "configured": true,
  "runtime": "claude", "model": null, "dir": "/Users/me/code/app", "repo": "", "tags": [],
  "state": "idle", "current_issue": null, "online": true,
  "plan_file": null, "plan_title": null,
  "tmux_created": "2026-08-31T12:00:00+00:00", "tmux_attached": false, "tmux_windows": 1,
  "last_activity": 1788177600.0, "state_since": 1788177590.0
}], "total": 1}
```

`state` is one of `offline`, `idle`, `busy`, `processing_issue`,
`plan_waiting`, `permission_waiting`, `starting`.

### `POST /api/agents/{name}/start`

Body (all optional): `{"runtime": "codex", "model": "…", "resume": true,
"working_directory": "/path"}`. Configured agents use their config; an
unconfigured name needs `working_directory`. Idempotent
(`already_existed: true`).

### `POST /api/agents/{name}/stop`

### `GET /api/agents/{name}/state`

The reconciled readiness snapshot for one session, with its `source`
(`push` = hook file, `pull` = terminal, `db`).

### `POST /api/agents/{name}/state`

Push state from outside (the same shape the hook writes): `{"state": "busy",
"issue": 42, "ts": 1788177600.0, "plan_file": "…", "plan_title": "…"}`. Use
this if you write hooks for a runtime the backbone does not ship hooks for.

### `GET /api/runtimes`, `GET /api/sessions`, `GET /api/sessions/{name}/terminal?lines=50`

Supported runtimes with availability; raw tmux session names; a one-shot
capture of a session's screen.

## Messages

### `POST /api/messages`

```json
{"target_session": "builder", "from_entity": "reviewer",
 "message": "Auth tests pass; please rebase.", "priority": false}
```

Response: `{"ok": true, "session": "builder", "outcome": "delivered"}`.
Outcomes: `delivered`, `agent_working`, `offline`, `user_interacting`,
`plan_waiting`, `permission_waiting`, `grace_period`, `delivery_failed`.
For direct messages every non-`delivered` outcome means the message was
**queued** and will be delivered when the agent is idle (or expire after 30
minutes).

This is the endpoint agents use to talk to each other.

## Issues (requires `[github]`)

| Route | Purpose |
|---|---|
| `GET /api/issues?state=open&for=reviewer&from=planner&type=bug&label=…&repo=owner/name` | List with priority scores |
| `GET /api/issues/{n}` | One issue |
| `POST /api/issues` `{"title","body","labels":["for:reviewer","task"],"repo"?}` | Create and immediately notify the `for:` targets (201) |
| `GET /api/issues/{n}/comments` | Comments with parsed `[from:X]` tags |
| `POST /api/issues/{n}/comment` `{"body"}` | Add a comment |
| `PATCH /api/issues/{n}` `{"state":"closed"}` | Close / reopen |
| `GET /api/issues/{n}/dependencies` | Sub-issues and parents |

`for:` labels are validated against configured agents (400 otherwise).
Without GitHub configured these routes return 503.

## Deliveries

`GET /api/deliveries?issue_number=&target_entity=&outcome=&limit=`,
`GET /api/deliveries/failed`, `GET /api/deliveries/stats`. Delivery rows are
recorded for issue/comment/PR deliveries (not for direct messages yet).

## Plans

`GET /api/plans` (agents in `plan_waiting`), `GET /api/plans/{name}` (with
the plan text), and — only when `[security] allow_remote_plan_control =
true` — `POST /api/plans/{name}/approve`, `/reject {"feedback"}`,
`/respond {"input"}`.

## Status

| Route | Purpose |
|---|---|
| `GET /health` (no auth) | Per-component health |
| `GET /api/status` | Digest: sessions, agents with state, pending issues, failed deliveries |
| `GET /api/status/services` | api/database/scheduler/telegram/github plus per-job run counts and last errors |
| `GET /api/config/agents` | The configured agents (non-secret) |

## Telegram

`POST /api/telegram/reply {"session": "reviewer", "text": "…"}` posts text into
the Telegram topic mapped to that agent (404 if none).

## Webhook

`POST /webhooks/github` (also `POST /`). Verified with
`X-Hub-Signature-256`; `X-GitHub-Delivery` is used for dedup; `ping` returns
`pong`.

## Socket.IO

Connect with `auth: {api_key: "<BACKBONE_API_KEY>"}`.

### Namespace `/sessions`

Server emits `sessions:update` with the same array as `GET /api/agents`
whenever an agent starts/stops/changes state (and at least once a minute from
the monitor). Subscribe and render; no client events needed.

### Namespace `/terminal` (read-only)

| Client → server | Payload |
|---|---|
| `join` | `{session, cols?, rows?}` — start streaming that session |
| `leave` | `{session}` |
| `resize` | `{session, cols, rows}` |
| `release_dims` | `{session}` — stop influencing tmux's window size (e.g. panel collapsed) |
| `pause` / `resume` | `{session}` — backpressure |

| Server → client | Payload |
|---|---|
| `terminal_output` | `{session, data}` — raw bytes with ANSI, coalesced |
| `session_ended` | `{session, reason}` |
| `data_dropped` | `{session}` — the client was too slow; redraw |
| `error` | `{message}` |

There is deliberately no `input` event.

## Example: a minimal dashboard loop

```js
const sio = io("http://127.0.0.1:7120/sessions", { auth: { api_key: KEY } });
sio.on("sessions:update", agents => render(agents));
```
