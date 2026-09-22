# HTTP & Socket.IO API

Base URL `http://127.0.0.1:7120`. Interactive OpenAPI docs at `/docs` while
running. Every route except `GET /health` and the webhook requires
`Authorization: Bearer <BACKBONE_API_KEY>`.

Response examples below show selected fields. Use the running OpenAPI schema
for complete request and response models.

## Progress reports

`GET /api/reports/schema` exposes the required sections, field and total limits,
and a synthetic example. `POST /api/reports` accepts `{agent, request_id, report}`
and returns `{record, created}` (201 new, 200 same retry). Validation failures are
422 with short field errors, oversized requests 413, key/content conflicts 409,
and the per-author publication bound 429. Report text is never silently truncated.

`GET /api/reports` returns the latest report per registered agent before limiting,
or history with `history=true`. Filters: repeatable `agent`, `members`, `limit`
(1–20), and `cursor`; use `author_id` with history for a forgotten author's records.
Responses contain `items`, `has_more`, and `next_cursor`. Each item carries
`agent_name`, `author_id`, and a full `record`, or null when no report exists.
`GET /api/reports/{id}` returns one complete report with age, authorship and links.

The named OpenAPI operations and [reporting reference](reports.md) describe these
agent-facing tools, bounded content, stable pagination, identity and retention.
Read operations query stored reports without prompting agents. Author names are supplied by
the authenticated client; there is no separate per-agent authentication boundary.

## Token usage

Authenticated `GET /api/usage` provides the same accounting as `backbone usage`.
Optional filters: `agent`, `runtime`, `session` (usage ID or unambiguous runtime
conversation ID), `since` inclusive and `until` exclusive (timezone-qualified
ISO timestamps), `current_only=true`. `by=session` is the default; `by=model`
aggregates observed models. Selecting a session returns request observations.
`refresh=false` reads history only; `reprice=true` previews current configured
prices without changing stored estimates. `limit=100` (1–1000) and `offset=0`
page `items`; `totals` always covers the full selection.

The response includes `items`, `sessions`, `totals`, `total_items`, `has_more`,
`next_offset`, `collection`, `unavailable`, `limits`, and accounting `semantics`.
Session metadata identifies the agent, CLI, conversation, launches, parent,
observed models, current state and source coverage. Request details retain
the disjoint token categories, available turn ID, provider, model and price
basis. `estimated_usd=null` means unpriced, never free. `limits` contains last
observed account-window snapshots, not per-session balances. Invalid filters
return 422; the normal API-key requirement applies. Full semantics and
coverage boundaries: [Token usage](token-usage.md).

## Agents

### `GET /api/agents`

Known agents plus any other live tmux session (except the backbone's own),
with live state. Cached for 5 s, including an empty result. Offline agents keep
saved issue metadata without attempting a terminal capture.

```json
{"items": [{
  "name": "app", "session": "app", "configured": true,
  "runtime": "claude", "model": null, "dir": "/Users/me/code/app",
  "repo": "acme/app", "watches": ["acme/web"], "tags": [], "description": "",
  "state": "busy", "reason": null, "current_issue": 42, "current_repo": "acme/app",
  "online": true, "plan_file": null, "plan_title": null,
  "tmux_created": "2026-08-31T12:00:00+00:00", "tmux_attached": false, "tmux_windows": 1,
  "last_activity": 1788177600.0, "state_since": 1788177590.0
}], "total": 1}
```

`state` is `offline`, `starting`, `idle`, `busy`, `waiting_for_human`,
`blocked` or `unknown`; `reason` is `plan`, `permission` or `question` when
waiting, `quota` or `provider` when blocked. `GET /api/agents/{name}/inspect` also carries
`session_id` (the runtime's own) and `last_message` (the agent's last reply,
clipped) when the runtime's hook reports them.
The `always_on` and `unattended` settings are exposed by `GET /api/config/agents`.
Agent responses label the saved model with `model_source: "configured"`.
That model is configuration metadata, not proof of which model or provider
actually generated a response in the running session.

### `POST /api/agents/start`

Starting an existing agent reuses its saved CLI and model and begins a **new
conversation**. Continuing the previous one is your call: `backbone agent resume
NAME` (or `start --resume`; API `resume: true`) reopens the session the backbone
last saw for that runtime, or the runtime's own last conversation when no ID is
saved. A fresh start says when a previous conversation is available. Starts are
fresh by default because a resumed agent trusts its own context over whatever
happened in the checkout since — another CLI, a swarm, the shared memory.
Changing runtime without specifying a model clears the previous runtime's model.
Starting from a directory reuses its registered name, even after a rename;
if several agents share that directory, specify a name.

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

Stops a registered agent. An unregistered tmux session returns 404.

### `POST /api/agents/{name}/restart`

Stops a registered agent's session and starts its replacement after a wait —
the one restart the backbone performs, because it was asked. The backbone
owns the transition: the request is validated and stored, the response is the
pending transition, and the `agent-transitions` job stops the session on its
next tick and starts the replacement when the wait is over, through the same
launch as `start` (record update, brief, hooks, readiness wait). Nothing inside
the session takes part after acceptance, so an agent can ask for its own
session and lose nothing; a backbone restart in between changes nothing either.

```json
{"runtime": "claude", "model": "opus", "resume": false, "start": true,
 "delay_seconds": 60, "start_at": null, "message": "Resume from HANDOFF.md",
 "from_entity": "app"}
```

All fields are optional. `runtime` and `model` (with an optional `:effort`)
are recorded on the agent as `start` would; omitted, the saved ones are used.
`resume` continues the saved conversation and is valid only when the runtime
is unchanged. `start: false` stops and starts nothing. The wait is between the
stop and the start: `delay_seconds` (default 60, at most 30 days) or `start_at`
(ISO 8601; local time unless an offset is given), not both. `message` is
delivered to the replacement once it is at its prompt, as
`[via:backbone from:<from_entity>]`, and never expires while it waits.

```json
{"id": 7, "session": "app", "requested_by": "app", "requested_at": "…",
 "runtime": "claude", "model": "opus", "resume": false, "start": true,
 "delay_seconds": 60, "start_at": null, "message": "Resume from HANDOFF.md",
 "status": "pending", "stopped_at": null, "completed_at": null, "result": {}}
```

`status` ends `completed` — `result` carries the launch's `ready` and
`evidence`, the effective runtime and model and, with a message, its
delivery outcome as `result.message` (`delivered`, or the [delivery
condition](concepts.md#delivery-condition) that queued it, such as `settling`)
with `result.message_queue` (`stored` or `already_queued`) when it waits in
the queue — or `failed` with `result.reason`: the runtime could not launch,
the session could not be stopped, the agent was forgotten, the continuation
message could be neither delivered nor stored, or a session was already
running at start time (someone started it by hand during the wait; it is
never claimed as this transition's work). A backbone restart in the middle
of the launch is recovered from the startup diagnostics: a replacement this
transition launched completes with the readiness recorded there. Errors:
`400` for a request that could not work (unknown or missing runtime, an effort
the runtime lacks, a cross-CLI resume, delay and time both given, an
unparsable time, the backbone's own session), `409` while the agent already
has a pending transition, `404` for an unregistered agent.

### `GET /api/agents/{name}/restarts?limit=5`

The agent's recent transitions, newest first, in the shape above.
`GET /api/agents/{name}/inspect` carries the open or latest one as `restart`.

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

`delivery` is the delivery condition: `ready`, `settling`, `human_typing`, `human_reading`,
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
it (`ts` defaults to now; explicit `0` also means now). Only registered agents
have a state file. Unknown state names, negative issue numbers, and non-finite,
negative or more than 60 seconds future timestamps return 422.

### `PATCH /api/agents/{name}`

The agent session feed (`GET /api/agents` and Socket.IO sessions updates) also
includes `last_message`, `detail`, `state_source` and `evidence`, when available.
CLI status uses those observations without polling GitHub for task estimates.

Change `dir`, `runtime`, `model`, `repo`, `tags`, `env`, `description`,
`always_on`, `unattended` (booleans; see [configuration](configuration.md#agents)).
Changing `runtime` clears `unattended` unless the same request sets it: a
freedom granted with one CLI's sandbox in mind does not follow the agent to
another.

### `POST /api/agents/{name}/tags`

`{"tags":["backend","python"],"remove":false}` adds tags without replacing the
others; `remove:true` removes only those tags. Names are printable, at most 100
characters, with no whitespace. `swarm:` and `role:` tags are lifecycle-managed.
Returns the updated agent configuration; unknown agents return 404, invalid
tags 400. Concurrent changes are serialized through the agent store.

### `POST /api/agents/{name}/rename`

`{"name":"new-name"}` renames a stopped non-swarm agent. Configuration, watches,
queued messages, routing receipts, explicit topic routes, escalation target and
the saved runtime conversation ID follow the new name. An occupied name, existing
history at the destination, an active delivery or active swarm participation
returns 409. External GitHub labels and scripts are not changed. See the
[CLI reference](cli.md#backbone-agent-) for session and topic behavior.

### `POST /api/agents/{name}/watch` · `/unwatch` `{"repo": "acme/web"}` · `DELETE /api/agents/{name}`

Watch / stop watching a repository; forget a stopped agent (409 if running).

### `POST /api/agents/{name}/subscribe` `{"source": "gmail", "filter": "from:upwork.com", "priority": "high"}` · `/unsubscribe` `{"id": 3}`

Subscribe to inbound events from a [source](sources.md), matched by a filter in
the source's own query language, at `normal` (default) or `high` priority; the
same filter again only changes its priority. Both return the agent's
configuration view with `subscriptions: [{id, source, filter, priority}]`;
400 for an unknown source or priority, 404 for an unknown agent or id.

### `GET /api/sessions/{name}/output?lines=20&before=OFFSET|since=OFFSET&end=OFFSET&screen=false`

A page of a registered agent's user-facing messages, reads only. `source` is
`transcript` when the runtime keeps a record the backbone can read (Claude
Code, Codex; located from the session id the hook recorded, only when that
record's runtime is the live one) and `screen` otherwise (`screen=true`, no
transcript, an unsupported runtime): the visible terminal with ANSI stripped
in `lines`.

Messages are the agent's own text to the person — progress, commentary and
replies — **complete**; tool calls, tool results and thinking are not
messages and are never included. A page holds at most `lines` messages
(max 200) and never cuts one. Navigation is by byte offset into the
append-only file: by default the last `lines` messages; `before=OFFSET`
the messages ending at or before it (earlier pages); `since=OFFSET` the
messages starting at or after it (later pages), with `end=OFFSET` bounding a
range. `range_start`/`range_end` are the page's offsets, `more_before` /
`more_after` say whether messages exist beyond it. One read scans at most
64 MiB of file; a page that hit that bound says so in `evidence` with the
offset to continue from. `400` when `since` and `before` are both given or
`end` comes without `since`; `404` for an unregistered agent.

```json
{"session": "app", "source": "transcript", "runtime": "claude",
 "messages": [{"time": "10:00:05", "role": "assistant", "text": "Looking at it now.", "start": 5120, "end": 6301}],
 "range_start": 5120, "range_end": 6301, "more_before": true, "more_after": false,
 "lines": [], "evidence": ["transcript /Users/me/.claude/projects/-Users-me-code-app/<session>.jsonl"]}
```

### `POST /api/steer`

```json
{"target_session": "app", "from_entity": "leo", "message": "the lock is taken by the retry job; wait for it"}
```

Guidance for the agent's **current task**, handed over through its runtime's
hook as a transient offer — never a queue row, never a paste. Accepted only
when the registered agent is `agent_working` on a runtime whose hook can add
context (Claude Code, Codex) in a session the backbone started; the offer is
written for that session only (`<state_dir>/context/<agent>/<launch_id>/`)
and the hook returns it as `additionalContext` on the next tool call, with the
envelope `[via:backbone from:<from_entity>] (steer for your current task) …`.

```json
{"ok": true, "session": "app", "outcome": "offered", "reason": null, "delivery_id": 9,
 "operation_id": "…", "launch_id": "…", "evidence": ["…"], "detail": "Offered to app's current turn; …"}
```

`outcome` is `offered`, `refused` (`reason` `not_working`, `unsupported_runtime`,
`offline` or `no_launch_id`; nothing is queued — send an ordinary message) or
`failed` (the offer could not be written). Status lives in the delivery log
(`kind` `steer`, `GET /api/deliveries?kind=steer&session=app`, `agent inspect`):
`offered` → `handed_off` once the hook took it, `not_taken` when the turn
ended first or no further tool call took it within 300 s (the offer is removed), or `cancelled` when the
session was replaced first. At-most-once handoff; a handoff is not
incorporation. `404` for an unregistered agent; `422` for a sender that does
not fit the envelope.

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
`human_typing`, `human_reading`, `settling`, `delivery_failed`.

The reply also includes `operation_id`, `delivery_id` and `queue_id` (nullable).
The operation identifies this message across queue drains and retries; a duplicate
enqueue returns the existing queue row and operation. `delivery_id` identifies the
attempt receipt, and `queue_id` identifies its stored queue row when present.
Use `GET /api/diagnostics/records?operation_id=...` to follow its operational evidence
without reading message content. A later independent send receives a new operation.

When the message could not be delivered now, `queue` says what happened to
it and `queued` is true **only when a row for it exists**:

| `queue` | `queued` | Meaning |
|---|---|---|
| `stored` | true | Kept; delivered when the agent is ready, or expired after `timing.queue_expiry_minutes` unless retained for active swarm coordination or inbox acknowledgement |
| `already_queued` | true | The same message from this `from_entity` is already waiting; nothing was added |
| `failed` | false | The database refused it — the message is not held anywhere; send it again later |

`detail` is the same information as one sentence, for a person or an agent
reading the reply. Two senders with identical text are two messages; the
same sender repeating the same text while the first copy waits is one.

`from_entity` must be nonblank, at most 64 characters, and contain no square
brackets, CR or LF; invalid senders return 422. The API key grants access, but
the supplied sender name is not authenticated identity.

This is the endpoint agents use to talk to each other.

## Config

| Route | Purpose |
|---|---|
| `GET /api/config` | Every setting with value, default and help |
| `GET /api/config/{key}` · `PUT /api/config/{key}` `{"value": …}` · `DELETE /api/config/{key}` | Read / set / reset one setting (published live; startup-only consumers require [restart](configuration.md)) |
| `GET /api/config/agents` | The known agents (non-secret) |

## Skills

The shared skills store ([skills](skills.md)). `GET /api/skills` lists every
store entry with its tags, validity and the agents it reaches.
`POST /api/skills` `{path, name?, tags, replace?, actor}` **moves** a skill
directory into the store and tags it (422 when the directory is not a skill or
the name is taken without `replace`; 400 when `skills.store` is empty).
`PUT /api/skills/{name}/tags` `{tags, actor}` replaces its tags (404 for an
unknown skill). `GET /api/skills/preview/{agent}` returns what that agent's next
launch links, per directory, with link state and notices. `actor` is recorded in
the store's commit history when the store is a git repository.

## Help and documentation

| Route | Purpose |
|---|---|
| `GET /api/help` · `GET /api/help/{topic}` | The agent playbooks (`setup`, `agents`, `messaging`, `github`, `swarms`, plus any under `<data_dir>/help/`) — index with one-line summaries, or one topic's markdown |
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

Issue lookup preserves an actual GitHub 404. Other upstream HTTP failures,
including rate limits and outages, return a sanitized 502.

## Deliveries and events

`GET /api/deliveries?repo=&issue_number=&kind=&target_entity=&session=&outcome=&limit=`,
`GET /api/deliveries/failed`, `GET /api/deliveries/stats`. Every delivery is
recorded, direct messages included, with `kind`, `repo`, `outcome`,
`source` (which code path made the attempt) and a `preview`.

`GET /api/events?repo=&limit=` — inbound GitHub events (webhook and poll),
newest first, each with `source`, `event_type`, `issue_number`, `sender`,
`summary`, `received_at`, `processed_at` and the routing `outcome`.

## Operational diagnostics

These endpoints return explicit operational metadata. They do not include
message bodies/previews, terminal output, assistant replies, event summaries,
command lines or raw exception text. See [Learning from local usage](diagnostics.md)
for the investigation workflow and limits.

### `GET /api/diagnostics?since=&agent=&limit=`

Groups warning/error diagnostic records by category, code, severity, agent,
runtime, repository and issue. `since` is an ISO timestamp with a timezone;
omitting it uses the last 24 hours. `limit` is 1–100, default 20. Totals are
calculated before the display limit.

```json
{
  "since": "2026-09-07T00:00:00.000000Z",
  "generated_at": "2026-09-08T00:00:00+00:00",
  "groups": [{
    "category": "delivery", "code": "submission_unconfirmed", "severity": "error",
    "agent_name": "app", "runtime": "shell", "repo": "", "issue_number": null,
    "first_seen_at": "2026-09-07T12:00:00.000000Z",
    "last_seen_at": "2026-09-07T12:05:00.000000Z",
    "occurrences": 2, "operation_count": 1, "sample_id": 42
  }],
  "total_groups": 1, "total_occurrences": 2, "has_more": false,
  "count_semantics": "retained occurrences for operation/code records last seen in interval",
  "coverage": {
    "earliest_retained_at": "2026-09-07T09:00:00.000000Z",
    "retention_days": 30, "write_failures_since_process_start": 0
  },
  "informational": {"deferred_agent_working": 3, "submitted": 1},
  "deliveries": {"attempts": 6, "outcomes": {"agent_working": 3, "delivery_failed": 2, "delivered": 1}},
  "queue": {"pending": 1, "in_progress": 0, "oldest_pending_at": "2026-09-07T12:00:00.000000Z"}
}
```

`since` selects diagnostics by last observation. Occurrence counts include
each selected operation/code record's retained earlier observations; they
are not exact counts inside the window. `informational` summarizes expected
waits and other info records with those same count semantics. Delivery
counts are attempts timestamped in the window. Queue metadata describes the
current pending/leased rows. `earliest_retained_at` is the earliest surviving
record, not the beginning of guaranteed continuous coverage.

### `GET /api/diagnostics/records`

Filters: `since`, `agent`, `category`, `severity` (`info`, `warning`, `error`),
`operation_id`, `limit` (1–100, default 100), `before_id` (positive integer).
Returns `{"items": [...], "has_more": false, "next_before_id": null}`.
Records are ordered by descending ID. When `has_more` is true, pass
`next_before_id` as `before_id` to read the next page. Because a repeated
observation updates an existing record, an increasing ID alone is not a
cursor for all new observations.
`backbone diagnostics trace OPERATION_ID [--json]` reads this endpoint with
the exact operation identity from a delivery receipt.

A record contains `id`, `operation_id`, `category`, `code`, `severity`,
`agent_name`, `source`, `runtime`, `model`, `repo`, `issue_number`, optional
`delivery_id`/`queue_id`/`event_id`, `first_seen_at`, `last_seen_at`,
`occurrences`, and `details`. Details are restricted to declared scalar
identifiers/codes, booleans and counters. Missing correlation references
remain null; they are not reconstructed from message content.

### `GET /api/diagnostics/{id}`

Returns `{"record": {...}, "operation_records": [...], "has_more": false}`.
The operation records share the selected record's exact `operation_id` and
are limited to 100. Use the records endpoint to page through more. Unknown
or pruned IDs return 404. A successful terminal submission is evidence of
submission; it does not prove the runtime accepted the prompt or a model
completed it. A retired retry or a runtime error no longer visible is not
proof that its original cause recovered.

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
| `GET /api/status/services` | api/database/scheduler/github, `integrations: {telegram: up \| down \| disabled}`, `sources: {gmail: enabled \| disabled}`, plus per-job run counts and last errors |

## Integrations

`POST /api/integrations/reply {"session": "app", "text": "…"}` posts an
agent's answer into its surface on every enabled integration (Telegram: the
topic mapped to it). Response `{"ok": true, "session": "app", "posted":
{"telegram": true}}`; 503 when no integration is configured, 404 when none
has a surface for that agent yet. Swarm workers and coordinators receive 403;
they send findings through the coordinator to the repository agent.
`backbone reply "…"` is the CLI form.
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

Malformed non-object payloads for terminal leave, resize, release, pause and
resume are ignored. There is deliberately no `input` event.

On restart, orphaned terminal viewer processes are signalled only when their
PID, recorded process start time and full tmux attach command still match.
Legacy PID-only records, malformed records and processes whose identity cannot
be read are skipped. The portable process check uses second-resolution start
times; it is not an atomic process handle across the check and signal.
Attachments use exact session names; an offline `app` never resolves to
`app-2`. Replacing or removing the same viewer attachment waits for its
previous PTY cleanup to finish.

```js
const sio = io("http://127.0.0.1:7120/sessions", { auth: { api_key: KEY } });
sio.on("sessions:update", agents => render(agents));
```

## Hold automatic upgrade restarts

`POST /api/upgrade/hold` accepts `{"operation_id":"<UUID>","enabled":true}` and
returns `{"held":true,"operation_held":true}`. This authenticated operation holds
the running process's automatic upgrade watcher, without changing saved settings.
`enabled:false` releases only that operation's hold; a manual restart clears all
holds. A restart already requested returns 409, and an unavailable watcher returns
503. The CLI uses this handshake before `upgrade --no-restart` changes any files.


## Cooperative inbox

See [message checkpoints](cli.md#cooperative-message-checkpoints) for safe mid-turn
coordination, acknowledgement and retention.

`POST /api/messages/inbox` accepts `{"session":"worker"}` and returns
`{"session":"worker","messages":[...]}` with `id`, `operation_id`, `message`,
`ack_token`, `sender`, `status`, and `enqueued_at`. Acknowledge with the same endpoint and
`{"session":"worker","acknowledge":["41:operation-id"]}`. Pass each complete `ack_token` returned by the inbox, not a numeric row ID.
Up to 20 tokens per acknowledgement; stale or mismatched tokens return 409, unknown session 404.
The existing shared API key is required. Session names are self-asserted, as with
ordinary messaging; this is not a per-agent authentication boundary.
