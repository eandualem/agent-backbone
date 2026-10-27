# Conversations

Every artifact an agent publishes has one conversation that lasts across all of
its versions. You ask about a Document, Response or progress report, the agent
responsible for it receives your question, and its reply (and any revision it
publishes because of it) appears in the same thread, beside the artifact.
Shared skills and policies have conversations too, so you can ask an agent to
explain one even though no agent published it.

Conversations exist today for Documents, Responses, progress reports, skills and
policies. Sheets and current plans follow; until then their routes return
`404 {"error": "unknown_artifact"}`.

## Identity

A conversation is keyed by the artifact's kind and stable ID:

| Kind | `artifact_id` | Responsible agent |
|---|---|---|
| `document` | `document_id` | Author of the latest revision |
| `response` | `conversation_id`: the first Response in its correction chain | Its author |
| `report` | The report's ID (reports have no corrections) | Its author |
| `skill` | Assigned when Backbone first reads the skill ([below](#skills-and-policies)) | The maintainer the operator mapped, if any |
| `policy` | Assigned when Backbone first reads the policy | The maintainer the operator mapped, if any |

Responses and reports carry `conversation_id` in their list and show output.
Reading a conversation that has never been used returns an empty list with its
responsible agent. When that agent has been forgotten, the header reports
`responsible.status: "retired"` and a message must name a recipient: Backbone
never routes to a new agent that merely reuses the name.

## Entries

A conversation is an append-only list of entries:

- **Owner messages**, delivered to a recipient, with a delivery state.
- **Agent messages**, optionally answering an entry (`in_reply_to`) and naming
  the version the reply produced (`resulting_revision_id`).
- **Revision events**, appended automatically when a correction is published,
  or when Backbone sees a skill or policy change (author type `backbone`).
  They record that a version appeared and who published it, never which
  message caused it.

Any message can carry an `anchor`: the version the author was viewing
(`revision_id`) and a display `label`. Artifact content stays in its own store;
entries only reference versions, and every version stays readable.

## Skills and policies

A skill or policy stays where it lives: the shared skills store, or the policy
templates edited with `backbone instructions`. Backbone reads it when it is
listed (`GET /api/skills`, `GET /api/policies`), changed through the running
backbone (`skills add`, `skills tag`), or when its conversation or version list
is read. A change made any other way (an editor, `skills add --local`) is
recorded at the next of these reads; there is no polling.

- **Identity.** The store and the item's name. How it is distributed (to every
  agent, or by tag) is not part of it, so retagging keeps the conversation, and
  a policy override and the bundled default are the same policy. Backbone
  assigns the `artifact_id` the first time it reads the item.
- **Versions.** Each time Backbone sees changed content it keeps an immutable
  snapshot (`v1`, `v2`, …) with its SHA-256 and the time it was observed. The
  first snapshot is where history starts: nothing earlier is invented, and
  edits made between two observations are not captured. A policy's version is
  the text agents receive. A skill's version is its definition text, the
  `SKILL.md` file only: scripts and other files in the skill change on their
  own and are not kept, so an old version is not the complete earlier skill
  and does not reproduce how it behaved.
- **Missing.** An item that cannot be read (deleted, or briefly absent during a
  save) is reported as `source_status: "missing"` and keeps its identity; when
  it can be read again, changed content becomes a new version.
- **Retired.** Only the operator ends an identity
  (`POST /api/conversations/{kind}/{artifact_id}/retire`). Retiring deletes
  nothing: the conversation and its versions stay readable by `artifact_id`,
  as do a missing item's, even when the listings no longer show the item.
  Anything later found under the same name gets a new identity. A rename is a
  new identity; the old conversation is never attached to it.
- **Responsible agent.** None by default (`responsible.status: "unassigned"`),
  so each message names its recipient, for example an agent who can explain the
  policy. The operator can map one registered agent as maintainer
  (`PUT …/maintainer {"agent": NAME}`, `DELETE …/maintainer` to clear); tags,
  paths and who last changed a file never make an agent responsible. Changing
  the item still goes through its own store.

## Delivery

An owner message is stored together with its delivery in one transaction, then
delivered through the message queue, so a busy agent is never interrupted. The
agent receives a short summary, not the whole thread:

```text
[via:backbone conversation:document/3f9… from:owner] On "Benchmark runs" (rev 2):
Could History come before column paging?
Reply: backbone conversations reply document 3f9… "…" --to 4182
```

`delivery` is `pending` (stored, queued or retrying), `delivered` (pasted, or
acknowledged from the inbox), `held` (waiting in the recipient's inbox) or
`failed` (expired or refused). A reply is not a delivery state; it is a later
entry. A failed message can be redelivered without adding an entry. If the
notification expires, the message stays in the conversation.

## Reading and replying as an agent

```bash
backbone conversations                         # conversations you are part of
backbone conversations show document 3f9…      # header and newest entries
backbone conversations reply document 3f9… "Done, see rev 3." --to 4182 --revision 3151
```

An agent reads and replies only where it is responsible, was addressed or has
written before, and posts as its own registration, with the same author and
signing checks as publishing a Document. Being addressed grants no edit rights:
revise the artifact through its own publish command, then name the new version
with `--revision`. Backbone refuses a revision that is not a version of this
artifact you published.

## API

| Call | Purpose |
|---|---|
| `GET /api/conversations` | Summaries for badges: `kind`, `artifact_id`, `responsible`, `unread`, `total`, `last_entry_at`. Paged with `after` and `limit` (50, at most 200); `since=CURSOR` returns only conversations changed after a previous `cursor`; filters `kind` and `ids` (up to 50 `kind:artifact_id` keys). |
| `GET /api/conversations/{kind}/{artifact_id}` | Header (`responsible`, `participants`, `total`, `unread`, `read_through`) and a page of entries, oldest to newest. `before=` pages older entries, `after=` returns newer ones for polling, and `entries=ID,…` (up to 50) returns those entries, oldest to newest, to refresh the delivery of older messages; an ID not in this conversation is left out. Sends an `ETag`; an unchanged poll with `If-None-Match` returns 304. |
| `POST /api/conversations/{kind}/{artifact_id}/messages` | Append a message: `request_id`, `text`, and optionally `recipient` (registration ID or name), `anchor`; agents add `author`, `in_reply_to`, `resulting_revision_id`. 201 when stored, 200 for an exact retry. |
| `POST /api/conversations/{kind}/{artifact_id}/read` | `{"read_through": ID}` advances the owner's marker (never backwards) and returns the new counts. |
| `POST /api/conversations/{kind}/{artifact_id}/messages/{entry_id}/redeliver` | `{"request_id": …}` queues a new delivery for a failed owner message: `queued`, `already_pending` or `refused`. |
| `GET /api/documents/{document_id}/revisions`, `/api/responses/{conversation_id}/revisions`, `/api/reports/{conversation_id}/revisions` | Versions newest first; read one exactly with the kind's existing read. |
| `GET /api/skills/{artifact_id}/versions`, `/api/policies/{artifact_id}/versions` | Snapshots newest first (`revision_id`, `label`, `created_at`, `sha256`); `…/versions/{revision_id}` adds `content`. |
| `GET /api/policies` | Every policy with `name`, `path` (the file agents receive it from: the override if there is one, otherwise the bundled default), `artifact_id`, `status` and `latest_revision_id`. `GET /api/skills` items carry `artifact_id`. |
| `PUT /api/conversations/{kind}/{artifact_id}/maintainer`, `DELETE …/maintainer` | Skills and policies: map `{"agent": NAME or registration ID}` as the responsible agent, or clear it. |
| `POST /api/conversations/{kind}/{artifact_id}/retire` | Skills and policies: end the identity. |

Owner messages, read markers, redelivery, maintainer mapping and retirement
need the operator credential. An agent credential reads as the agent it names
(the client's agent header, or
`?agent=NAME`).

`unread` counts agent messages and events after `read_through`; the owner's own
messages never count. `total` counts messages only.

A refused send returns `{"result": "refused", "error": CODE, "message": …}`:

| Code | Meaning |
|---|---|
| `too_long` | Text over 4,000 characters |
| `rate_limited` | Over `conversations.owner_messages_per_hour` |
| `conversation_full` | At `conversations.max_messages`; old messages are never dropped |
| `recipient_required` | No active responsible agent (retired, or unassigned for a skill or policy); name a recipient |
| `unknown_recipient` | No registered agent by that name or ID |
| `invalid_revision` | An anchor or `resulting_revision_id` is not a version of this artifact by this author |
| `invalid_reply` | `in_reply_to` names no entry of this conversation |
| `not_participant` | The agent is not part of this conversation |
| `request_conflict` | The `request_id` was used for a different message |

A missing artifact is `404 {"error": "unknown_artifact"}`; a Backbone without
conversations returns a plain 404.

## Limits and retention

Messages are at most 4,000 characters. The message and rate limits are
[settings](configuration.md#conversations--artifact-conversations). Revision
events are never refused, so publishing is never blocked by a full
conversation. Conversations, Documents, Responses and reports are never pruned.
