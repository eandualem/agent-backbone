# Interactions wire v1 — isolated candidate

The JSON schemas describe the candidate response objects; fixture files are
illustrative, not captured owner history. These endpoints target an independent branch for testing, not a released
capability. The provider is implemented on the isolated candidate branch. Consumer and owner
acceptance remain pending; this is not a released capability.

- `GET /api/interactions`: required `agent_id`, `since`, `until`; optional
  `direction=both|incoming|outgoing`, `limit` (50, max 200), `cursor`, `snapshot`.
  Returns `InteractionPage`. Window is UTC-normalized **[since, until)**.
  Include an interaction if any lifecycle event occurs in the window or its
  pending lifecycle spans the window; do not filter solely by acceptance time.
- `GET /api/interactions/summary`: required `since`, `until`; optional
  `agent_id`, `snapshot`. Returns `Summary`. Without agent ID, all current
  registrations appear, including offline and empty-window registrations, plus
  retired participants with retained captured history. Current name/runtime/
  state are separate from captured names. `previous` is the most recent
  interaction strictly before the window; `latest` is the latest in the window.
- `GET /api/interactions/{id}` returns `Interaction` independent of a window.
- `GET /api/interactions/{id}/attempts` returns `AttemptPage`;
  `GET /api/interactions/{id}/events` returns `EventPage`. Both accept `cursor`,
  `limit` (50, max 200). Nested pages explicitly expose `next_cursor`.
- Conversation routes use kind **`interaction`**, never `message`.
  `conversation.supported` and `conversation_supported` gate the consumer.
  The original message is the artifact; post a question to an explicit member
  of its `recipients`, through existing conversation POST semantics.

History reads require the operator credential. Conversation reads/writes retain
operator and stable-participant checks. Original bodies are immutable while
retained; excerpts/unavailable bodies are explicit. Unknown sender IDs stay null.
An owner actor must be explicitly attributed through an owner-authorized path,
not guessed from a shared operator credential or name. Optional new send field
`interaction` uses `InteractionInput`: `actor=owner` requires operator authority;
relations are validated recorded links, never inferred. Existing send inputs
remain valid.

`counts.sent` counts logical messages from the agent; `counts.received` counts
logical messages delivered to it (including explicit ACK or hook handoff);
`counts.not_delivered` counts relevant messages with no recorded successful
handoff. Self-sends contribute once to sent and, if delivered, once to received;
not-delivered counts once. Attempts never inflate these counts. Ticks retain
logical-message IDs and the time used for their display; their `time_basis`
labels recorded/observed/legacy time. `ticks_truncated` is explicit.

Relations have typed targets, including reports/conversation entries/corrections
and owner requests; empty means unrecorded. A `report` target ID is the report
record ID, with `artifact_kind=report|response` selecting its existing reader. A `conversation` target has its artifact kind/ID and optional entry
ID. An `owner_request` target is an interaction with an owner sender. Corrections
and replies target retained interactions. No inferred causal turn link exists.

Cursors are opaque, scoped and snapshot-bound. Refresh starts a new snapshot;
invalid/mismatched/stale cursors return 422. A 404 on the route means unsupported
on an old provider; a detail 404 means that specific record is absent. All
history coverage is explicit; the original bodies of old traffic cannot be
reconstructed. State context remains independently observed and partial.

Candidate retention: full bodies follow delivery retention (30 days by default).
An interaction referenced by a conversation keeps immutable context, and is not
silently orphaned by transport pruning. Retained identity metadata does not
claim complete historical coverage. Tests run only on disposable databases;
no production migration/deployment follows from this contract.

Recorded relation responses include `source_interaction_id`. A detail also
projects incoming recorded links (for example, messages explicitly made for the
selected owner request); preserve the actual source and target rather than
inventing an inverse relation. `relations_truncated` signals more than 200
referring records. The history page has a 1 MiB soft byte bound (one original
message is always returned); continue with its cursor. Summary scans at most
10,000 retained records and 200 ticks per agent, explicitly setting coverage
truncation and `ticks_truncated` when limited.

Interaction conversation reads additionally include `original_context` with the
immutable ID, accepted time, captured actors and retained body. Verified
participants can inspect this through the conversation without gaining operator
history access. It is quoted message data, not a new instruction. There is no
editable/resulting revision on an original message. A new explanation is written
now, not backdated to the original send.

`capture_started_at` and `retained_from` bound recorded history. The separate
`body_retained_from` bounds uniformly retained full bodies; older metadata,
excerpts and conversation-anchored originals may remain. Do not use the body
boundary as a history-start edge. Current transport status/counts are observed
as of the snapshot, even when inspecting a historical window.


## Observed state and chronological discussion entries (additive)

`GET /api/interactions/state-spans` requires `since`, `until`; optional `agent_id`
selects one registration. Omitting it returns one bounded all-registration page
for list-card strips, with stable agent_id on each span. It
accepts `snapshot`, `cursor`, `limit` (200, maximum500). It returns
`StateSpanPage`, oldest first. This captures registration-bound observations
prospectively from the existing SessionFeed, with the registration captured
before reading state and checked again when recording it. Legacy name-only
history is excluded; retirement and name reuse never reassign observations.

A `matching_samples` span lies between two matching recorded state/reason
observations from the same recorder, at most120seconds apart. It is a sampled
estimate, not continuous measurement. A changed state, restart or larger gap
is not bridged. `point_observation` has equal since/until and supplies a real
sample without inventing duration. Everything outside returned spans remains
unknown; follow every page before treating the returned set as exhaustive.
`observation_ids`, actual first/last observation times and evidence stay
inspectable. State coverage is always `sampled_partial` or `unavailable`.
Unchanged samples are retained at most once per minute; changes are retained
as observed. Existing monitor/API feed refreshes supply observations; there
is no new background scheduler. Samples and capture checkpoints retain7days.

Message coverage is independent. `coverage.message_intervals` lists finite
intervals proven covered by the capture-enabled Backbone paths in
`declared_scope`. Adjacent successful recorder checkpoints, at most120seconds
apart, bound each interval. Restarts, missing heartbeats, uncaptured transport,
legacy pending messages without originals and projection truncation prevent
an unqualified complete-window claim. Overall completeness is
`complete_within_declared_scope` only if the whole requested window is covered
and the projection is not truncated. Body excerpt retention is independent.

The consumer may assert **no captured Backbone messages** only within a
returned complete message interval, after reading all unfiltered history
pages for the registration and verifying no relevant logical messages overlap
that interval. Partial sampled state does not negate complete message capture,
and complete message capture does not prove continuous state or exclude
private conversations outside Backbone. A current window can have a complete
historical prefix and an unknown tail since the last checkpoint. Before the
first checkpoint, and outside retained checkpoints, quiet remains unknown.

`Interaction.conversation_entry` is nullable. When present it identifies the
actual source conversation entry: kind/artifact_id, stable entry_id,
entry_kind, created_at, author_type/author_id/author_name, recipient_id/name,
in_reply_to, stored anchor, and anchored_interaction_id when kind=interaction.
Owner author_id follows the existing conversation entry convention (empty
string plus author_type=owner); the interaction's owner Actor remains explicit.
Only a capture-generated source-entry relation supplies this field; a sender's
reference to an entry does not turn their message into that entry. Retried
transport retains the same entry identity/time. Use (kind,artifact_id) to group
only contiguous rows: A/B/A remains three runs, each with its actual entries
and times. Revision/status events are not fabricated as discussion messages.
Existing older conversation entries remain readable through their authorized
conversation API but are not backfilled into missing interaction history.

Unknown/unverified actor lifecycles are also excluded from quiet-safe intervals:
the provider cannot silently assume an unbound sender is unrelated to the selected
registration. Known intervals before/after that uncertainty remain available.
