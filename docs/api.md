# HTTP & Socket.IO API

Base URL `http://127.0.0.1:7120`. Interactive OpenAPI docs at `/docs` while
running. Every route except `GET /health` and the webhook requires
`Authorization: Bearer <BACKBONE_API_KEY>`.

## Agents

### `GET /api/agents`

Known agents plus any other live tmux session (except the backbone's own),
with live state. Cached for 5 s.

```json
{"items": [{
  "name": "app", "session": "app", "configured": true,
  "runtime": "claude", "model": null, "dir": "/Users/me/code/app",
  "repo": "acme/app", "watches": ["acme/web"], "tags": [], "description": "",
  "always_on": false, "unattended": false,
  "state": "busy", "reason": null, "current_issue": 42, "current_repo": "acme/app",
  "online": true, "plan_file": null, "plan_title": null,
  "tmux_created": "2026-08-31T12:00:00+00:00", "tmux_attached": false, "tmux_windows": 1,
  "last_activity": 1788177600.0, "state_since": 1788177590.0
}], "total": 1}
```

`state` is `offline`, `starting`, `idle`, `busy`, `waiting_for_human`,
`blocked` or `unknown`; `reason` is `plan`, `permission` or `question` when
waiting, `quota` when blocked. `GET /api/agents/{name}/inspect` also carries
`session_id` (the runtime's own) and `last_message` (the agent's last reply,
clipped) when the runtime's hook reports them.

### `POST /api/agents/start`

```json
{"dir": "/Users/me/code/app", "name": null, "runtime": null, "model": null,
 "resume": false, "watch": ["acme/web"], "wait": true}
```

Discovers (or re-registers) the agent for `dir`, starts it and — with
`wait` — blocks until it is at its prompt (up to
`timing.start_timeout_seconds`). Without `dir`, `name` must be a known agent.

```json
{"ok": true, "session": "app", "name": "app", "working_directory": "/Users/me/code/app",
 "runtime": "claude", "model": null, "repo": "acme/app", "already_existed": false,
 "ready": "ready", "evidence": ["hook reported idle 0s ago"]}
```

`ready` is `ready`, `waiting_for_human` (the runtime is asking something —
`evidence` shows the question), `timeout`, `exited` or `not_waited`.

### `POST /api/agents/{name}/start`

Same body; starts a known agent (`dir` in the body registers it first).

### `POST /api/agents/{name}/stop`

### `POST /api/agents/{name}/approve`

Body (optional): `{"from_entity": "orch"}`. Answers the permission prompt
the registered agent's runtime is showing — the runtime's affirmative key,
sent only while the dialog is visible. Response
`{"ok": true, "session": "app", "outcome": "approved", "evidence": ["..."], "approved_by": "orch"}`;
the evidence quotes the dialog and whether it cleared. Errors carry
`{"outcome", "evidence"}`: `409 not_waiting` (no prompt on screen — nothing
typed), `400 unsupported` (no verified answer for that runtime),
`404 offline`, `403` when `security.allow_remote_approval` is off. Every
approval is an `approval` event in `GET /api/events`.

### `POST /api/agents/{name}/deny`

Same body. Refuses the prompt with the runtime's refusing key (Escape for
Claude Code and Codex), under the same gate: only a dialog on screen is
answered, and the denial is recorded as a `denial` event. Response
`{"ok": true, "session": "app", "outcome": "denied", "evidence": ["answered with Escape; prompt cleared"],
"denied_by": "orch"}`. `approve` answers `409 not_permission` for a
*choice* dialog (Codex's rate-limit model switch, where Enter would pick
rather than allow); `deny` is the answer that keeps things as they are.

### `GET /api/agents/{name}/inspect`

Everything the backbone knows about one agent, with the evidence:

```json
{"name": "app", "known": true, "online": true, "dir": "…", "runtime": "claude",
 "model": null, "repo": "acme/app", "watches": [],
 "state": "busy", "reason": null, "current_issue": 42, "current_repo": "acme/app",
 "state_source": "push", "state_age_seconds": 4.4, "delivery": "agent_working",
 "evidence": ["runtime: claude", "hook state 'busy' written 4s ago (fresh)"],
 "tmux": {"pane_in_mode": "0", "…": "…"}, "pane_tail": ["❯ …"],
 "recent_deliveries": [{"kind": "direct_message", "outcome": "agent_working", "…": "…"}]}
```

`delivery` is the delivery condition: `ready`, `settling`, `human_typing`,
`agent_working`, `waiting_for_human`, `offline` or `unknown`.

### `GET /api/agents/{name}/state`

The reconciled state snapshot with `source` (`push` = the state file, written
by a hook or by `POST /api/agents/{name}/state`; `pull` = terminal) and
`evidence`.

### `POST /api/agents/{name}/state`

Push state from outside (same shape the hook writes): `{"state": "busy",
"reason": null, "issue": 42, "repo": "acme/app", "ts": 1788177600.0,
"plan_file": "…", "plan_title": "…"}`. Use it for runtimes the backbone does
not ship hooks for. It writes `<data_dir>/state/<name>.json` exactly as a
hook would, so delivery decisions, the monitor and `agent inspect` all see
it (`ts` defaults to now). Only registered agents have a state file.

### `PATCH /api/agents/{name}`

Change `dir`, `runtime`, `model`, `repo`, `tags`, `env`, `description`,
`always_on`, `unattended` (booleans; see [configuration](configuration.md#agents)).
Changing `runtime` clears `unattended` unless the same request sets it: a
freedom granted with one CLI's sandbox in mind does not follow the agent to
another.

### `POST /api/agents/{name}/watch` · `/unwatch` `{"repo": "acme/web"}` · `DELETE /api/agents/{name}`

Watch / stop watching a repository; forget a stopped agent (409 if running).

### `GET /api/runtimes`, `GET /api/sessions`, `GET /api/sessions/{name}/terminal?lines=50`

Supported runtimes with availability; raw tmux session names; a one-shot
capture of a **registered agent's** screen (404 for any other tmux session —
the API never reads or types into sessions that are not backbone agents).

## Messages

### `POST /api/messages`

```json
{"target_session": "web", "from_entity": "app",
 "message": "Auth tests pass; please rebase.", "priority": false}
```

Response: `{"ok": true, "session": "web", "outcome": "delivered", "queued":
false, "queue": null, "detail": "Delivered to web."}`. The target must be a
registered agent or an active swarm (404 otherwise — the backbone never
types into a tmux session that is not one of its agents). Outcomes:
`delivered`, `agent_working`, `waiting_for_human`, `offline`, `expired`,
`human_typing`, `settling`, `delivery_failed`.

When the message could not be delivered now, `queue` says what happened to
it and `queued` is true **only when a row for it exists**:

| `queue` | `queued` | Meaning |
|---|---|---|
| `stored` | true | Kept; delivered when the agent is ready, or expired after `timing.queue_expiry_minutes` |
| `already_queued` | true | The same message from this `from_entity` is already waiting; nothing was added |
| `failed` | false | The database refused it — the message is not held anywhere; send it again later |

`detail` is the same information as one sentence, for a person or an agent
reading the reply. Two senders with identical text are two messages; the
same sender repeating the same text while the first copy waits is one.

This is the endpoint agents use to talk to each other.

## Config

| Route | Purpose |
|---|---|
| `GET /api/config` | Every setting with value, default and help |
| `GET /api/config/{key}` · `PUT /api/config/{key}` `{"value": …}` · `DELETE /api/config/{key}` | Read / set / reset one setting (applied live) |
| `GET /api/config/agents` | The known agents (non-secret) |

## Help and documentation

| Route | Purpose |
|---|---|
| `GET /api/help` · `GET /api/help/{topic}` | The agent playbooks (`setup`, `agents`, `messaging`, `github`, `swarms`, plus any under `<data_dir>/help-topics/`) — index with one-line summaries, or one topic's markdown |
| `GET /api/docs` · `GET /api/docs/{page}` | The documentation shipped with the installed package (`getting-started`, `concepts`, …) — index, or one page's markdown |

The same content as `backbone help` and `backbone docs`, for agents that
reach the backbone over HTTP.

## Issues (requires GitHub credentials)

Every route takes `repo=owner/name`.

| Route | Purpose |
|---|---|
| `GET /api/issues?repo=…&state=open&for=app&from=orch&type=bug&label=…` | List with priority scores |
| `GET /api/issues/{n}?repo=…` | One issue |
| `POST /api/issues` `{"repo","title","body","labels":["for:app","task"]}` | Create and immediately notify the `for:` targets (201) |
| `GET /api/issues/{n}/comments?repo=…` | Comments with parsed `[from:X]` tags |
| `POST /api/issues/{n}/comment?repo=…` `{"body"}` | Add a comment |
| `PATCH /api/issues/{n}?repo=…` `{"state":"closed"}` | Close / reopen |
| `GET /api/issues/{n}/dependencies?repo=…` | Sub-issues and parents |

`for:` labels are validated against known agents (400 otherwise). Without
GitHub credentials these routes return 503.

## Deliveries and events

`GET /api/deliveries?repo=&issue_number=&kind=&target_entity=&session=&outcome=&limit=`,
`GET /api/deliveries/failed`, `GET /api/deliveries/stats`. Every delivery is
recorded, direct messages included, with `kind`, `repo`, `outcome`,
`source` (which code path made the attempt) and a `preview`.

`GET /api/events?repo=&limit=` — inbound GitHub events (webhook and poll),
newest first, each with `source`, `event_type`, `issue_number`, `sender`,
`summary`, `received_at`, `processed_at` and the routing `outcome`.

## Plans

`GET /api/plans` (agents waiting for plan approval), `GET /api/plans/{name}`
(with the plan text — read only from `<state_dir>/plans/`, never from an
arbitrary path the state record names), and — only when
`security.allow_remote_plan_control` is on and `{name}` is a registered
agent — `POST /api/plans/{name}/approve`, `/reject {"feedback"}`,
`/respond {"input"}`.

Approve and reject send the agent's **runtime's own** plan keys
(`Runtime.plan_approve_keys` / `plan_reject_keys`; Claude Code today). A
runtime without a plan mode the backbone can drive answers **409** and
nothing is typed. The response text (`/respond`) is a `plan_response`
delivery through `safe_deliver`: it goes in only while the agent is
waiting for a plan decision (`not_waiting` otherwise — a bare option
number at an idle prompt would be a new instruction), is recorded like
every other delivery, and is never queued — a 409 names the outcome.
`not_waiting` occurs only here, never on `POST /api/messages`. Rejection
feedback is not a plan response: it is sent *after* plan mode is left, as
an ordinary `direct_message` (enveloped, queued if the agent is busy); the
reply's `feedback` field is its outcome.

## Status

| Route | Purpose |
|---|---|
| `GET /health` (no auth) | Per-component health |
| `GET /api/status` | Digest: sessions, agents with state, GitHub intake mode, tracked repositories (owners, watchers, last event), pending issues, failed deliveries |
| `GET /api/status/services` | api/database/scheduler/github, `integrations: {telegram: up \| down \| disabled}`, plus per-job run counts and last errors |

## Integrations

`POST /api/integrations/reply {"session": "app", "text": "…"}` posts an
agent's answer into its surface on every enabled integration (Telegram: the
topic mapped to it). Response `{"ok": true, "session": "app", "posted":
{"telegram": true}}`; 503 when no integration is configured, 404 when none
has a surface for that agent yet. `backbone reply "…"` is the CLI form.
See [Integrations](integrations.md).

## Webhook

`POST /webhooks/github` (also `POST /`). Verified with
`X-Hub-Signature-256`; `X-GitHub-Delivery` deduplicates; `ping` returns
`pong`.

## Socket.IO

Connect with `auth: {api_key: "<BACKBONE_API_KEY>"}`.

### Namespace `/sessions`

Server emits `sessions:update` with the same array as `GET /api/agents`:
a full snapshot on connect, then again whenever an agent
starts/stops/changes state. Updates are change-only — an unchanged system
emits nothing (the monitor job re-checks once a minute).

### Namespace `/terminal` (read-only)

| Client → server | Payload |
|---|---|
| `join` | `{session, cols?, rows?}` — start streaming that registered agent (`error` for any other session) |
| `leave` | `{session}` |
| `resize` | `{session, cols, rows}` |
| `release_dims` | `{session}` — stop influencing tmux's window size |
| `pause` / `resume` | `{session}` — backpressure |

| Server → client | Payload |
|---|---|
| `terminal_output` | `{session, data}` — raw bytes with ANSI, coalesced |
| `session_ended` | `{session, reason}` |
| `data_dropped` | `{session}` — the client was too slow; redraw |
| `error` | `{message}` |

There is deliberately no `input` event.

```js
const sio = io("http://127.0.0.1:7120/sessions", { auth: { api_key: KEY } });
sio.on("sessions:update", agents => render(agents));
```
