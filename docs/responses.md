# Save an answer for later

Use a **Response** when a person should be able to find your answer while you
continue working. Supply the question (or a faithful short version), the direct
answer, and the resulting action or remaining decision. Nothing is extracted
automatically from your conversation.

```bash
backbone respond --example > answer.json
# Replace the example with the actual question, answer and action.
backbone respond --file answer.json --validate
backbone respond --file answer.json
backbone responses --agent app
backbone responses show 123
```

The file has three required plain-text fields:

```json
{
  "question": "Is the change ready to use?",
  "answer": "The fix is merged and its checks passed.",
  "action": "Try the updated command. No decision is needed."
}
```

The question allows 500 characters, the answer 6,000 and the action 1,000.
Paragraphs and tabs are allowed; terminal control characters and bidirectional
text controls are rejected. A request is bounded to 65,536 UTF-8 bytes. `--schema`
shows field limits. `--file -` reads stdin. These tools work through the same
authenticated API for every supported runtime; they do not start a model call
or interrupt an agent.

## Publish and correct

Inside an agent session, `$BACKBONE_AGENT` supplies the author. Other trusted
local callers pass `--agent NAME`. This is a supplied author name, not a separate
authentication identity. Enrolled sender names retain the existing
[signed-request requirement](owner-confirmed-messages.md#signed-requests). Swarm participants cannot publish human-facing
responses: send findings to the initiating repository agent.

Publication returns a stored ID and delivery state. Use `--json` for the complete
receipt. Retrying identical content reuses a hash-based key and returns the
original record; `--key KEY` sets an explicit retry key. Reusing a key with
changed content returns a conflict. Each author can publish up to 30 new
responses per rolling hour, separately from the progress-report allowance.

To update an answer, publish the complete revised question, answer and action:

```bash
backbone respond --file revised-answer.json --supersedes 123
```

The referenced response must still be retained and belong to the same author.
This appends a new record and leaves the original intact. Reads show the explicit
prior ID and the latest direct correction ID, when present. Association is never
guessed from words or runtime conversation identifiers.

## Read independently of progress

`backbone responses` lists retained answers newest first. Select an author with
`--agent NAME`, page with `--limit 1–100` and the returned `--before ID`, or use
`responses show ID` for a complete record. `--json` returns every field, including
text and audio delivery states. Responses are excluded from `backbone updates`;
ordinary `backbone reply` and progress reports keep their existing behavior.

Author identity survives a rename. Forgotten authors' retained responses remain
readable by ID and in the unfiltered list. Registering their old name does not
claim their history. The existing six-hour prune job uses
`timing.delivery_retention_days` (30 days by default), keeping each author's
latest response as well as their latest report. Older answers, correction
targets and retry keys can expire. Store anything needed longer elsewhere.

## Telegram delivery and audio

A response queues its full text for the author's existing configured or
discovered topic in the allowlisted agents group. It never falls back to General
or a private chat. If the topic is unavailable, delivery remains pending.
Headers use the original publication name so a rename cannot shift chunk boundaries.
Long answers are split into continuation messages carrying the response ID;
joined emoji, flags and combining marks stay together unless a sequence itself
exceeds the message size budget. Deliveries already in progress retain their
original chunk boundaries. Each successful chunk is checkpointed before continuing. A retry sends the
remaining chunks. A crash after Telegram accepts a chunk but before its receipt
is saved can still duplicate that chunk. The response ID identifies it.

Explicit responses continue when `telegram.report_updates=false`; that switch
pauses progress notifications only. Publication is not proof of Telegram
delivery: read `telegram_delivery` (`pending`, `sending`, `sent` or
`not_requested`). Forgotten authors and swarm participants have pending
deliveries retired. Normal retention still applies to pending answers.

`telegram.report_audio=true` also enables response audio through the existing
[local speech setup](report-audio.md). It speaks the complete question, answer
and action after all text chunks have been delivered. Its independent
`telegram_audio_delivery` state and retries never delay or resend the text.
Speech size and timeout limits still apply; a long answer may remain text-only
if audio generation fails.

## API

All routes require the existing API key:

- `GET /api/responses/schema`: content and publication schemas.
- `POST /api/responses`: `{agent, request_id, response: {question, answer, action}, supersedes_id?}`.
  Returns `{record, created}`, HTTP 201 for a new response or 200 for an identical retry.
- `GET /api/responses?agent=NAME&limit=20&before_id=ID`: `{items, has_more, next_before_id}`;
  `agent` and exclusive `before_id` are optional; limit defaults to 20, maximum 100.
- `GET /api/responses/{id}`: one complete retained response; HTTP 404 if absent.

Each record includes `id`, `author_id`, original `author_name`, current
`agent_name` (null if forgotten), `created_at`, `request_id`, `source`, `response`,
`supersedes_id`, `superseded_by`, `age_seconds`, `telegram_delivery` and
`telegram_audio_delivery`. Reads do not prompt an agent or send a Telegram message.
