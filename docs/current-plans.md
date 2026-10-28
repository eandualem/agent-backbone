# Keep an agent's current plan visible

Use a Current plan for a short checklist of what the agent is working on now,
what comes next and what can wait. The agent authors the checklist and checks off
completed items. The person reads it and sends questions or requests to change
priorities. It is separate from runtime status, native plan-approval dialogs,
progress reports and longer-lived spreadsheet trackers.

Refresh when work changes and daily when useful. Carry unfinished items and their
IDs forward. Remove completed items when moving to new work; use a new plan ID for
a new plan. Nothing clears at midnight, and Backbone does not choose, schedule,
execute or complete the work. An empty item list means no current items; a missing
record means the agent has never published a retained plan. An update at least
24 hours old is marked stale, not complete.

## Publish and read

First discover the registered agent's stable author ID:

```bash
backbone current-plan --agent example-agent --json
backbone current-plan publish --schema
backbone current-plan publish --file plan.json --validate --json
backbone current-plan publish --file plan.json --json
backbone current-plan show AUTHOR_ID --json
```

The request file contains `author`, `author_id`, `request_id`,
`base_revision_id` and `plan`. Use the actual registered name and author ID from
discovery. All other IDs below are illustrative; generate fresh UUID hex strings
for a new request, plan and item, then retain the submitted request for retries.
Files or stdin (`--file -`) are client input, not required repository artifacts.

```json
{
  "author": "example-agent",
  "author_id": "11111111111111111111111111111111",
  "request_id": "22222222222222222222222222222222",
  "base_revision_id": null,
  "plan": {
    "plan_id": "33333333333333333333333333333333",
    "title": "Validate the next change",
    "items": [{
      "item_id": "44444444444444444444444444444444",
      "text": "Run the behavior checks",
      "section": "now",
      "status": "in_progress",
      "detail": "",
      "references": [{"kind": "issue", "target": "example/app#42"}]
    }]
  }
}
```

First publication requires `base_revision_id: null`. Later updates replace the
whole short plan atomically and must name its current revision. Preserve continuing
item IDs. Array order is authoritative: `now` items first, then `next`, then
`later`. Statuses are `pending`, `in_progress`, `blocked` and `done`; optional
`detail` names dependencies, peers the item is waiting on, or other useful context. References use `issue` with
`owner/repo#number`, `sheet` with its UUID hex ID, or `document` with a positive
decimal revision ID. References are stored as data, not fetched or verified.

An accepted update returns `{receipt:{author_id,plan_id,revision_id,updated_at},
created:true}`. A retry of the identical request/key returns the original receipt
and `created:false`, even after later updates. A changed request under the same
key returns `409 request_conflict`. A stale base returns `409 stale_revision`
with current IDs; retain the draft, read the current plan and explicitly reconcile
it. There is no silent overwrite or automatic rebase. Refreshing identical content
with a new key and current base is allowed and updates freshness.

Publication uses the existing authenticated author route and signed-sender
requirements for enrolled names. The CLI does not supply missing signatures or
recover an operator key. The API has no anonymous/operator checklist-edit shape:
publication always names a registered author. Unenrolled author names still use
the existing trusted local-client model, not a new cryptographic identity boundary.
Rename preserves ownership; an old publication name must be reconciled after a
rename. Forgetting an agent never transfers its plan to a reused name.

## Questions and reprioritization

```bash
backbone current-plan feedback AUTHOR_ID --schema
backbone current-plan feedback AUTHOR_ID --file question.json --validate
backbone current-plan feedback AUTHOR_ID --file question.json --json
```

Feedback has `author` (registered agent name, or null for an operator),
`request_id`, `observed_plan_id`, `observed_revision_id`, `item_ids`,
`kind: question|feedback` and `text`. An empty selection targets the whole plan.
Normal paragraphs and tabs are allowed. Operator feedback requires an operator
credential; agent credentials cannot claim `author:null`.

Feedback captures the observed title and selected item contents and enters the
ordinary registration-bound message queue in one transaction. A changed revision
or replaced plan returns `409 stale_revision` and queues nothing. Keep the draft,
refresh and explicitly reselect items. A retired recipient returns
`409 owner_unavailable`; a replacement using its name never receives that feedback.

The result has `feedback_id`, `author_id`, `plan_id`, `observed_revision_id`,
`operation_id` and the ordinary `delivery` receipt. Exact retries keep the same
feedback/operation identity and return current delivery status, even after the
plan changes or its owner retires. They do not send a second message. Queued,
uncertain, expired and unknown are distinct; none means the agent answered.
After uncertain transport, preserve the exact request/key and retry it. Each
attempt still requires a current registered sender and any enrolled signature.
If the sender was renamed, the original name returns `author_unavailable`;
changing the name under the same key returns `request_conflict`. Reconcile delivery
history before deliberately creating a new operation. Changing the key is not a
safe automatic retry. Renaming the recipient preserves its registration-bound
context and does not prevent an unchanged sender from retrying.

## API and retention

All routes use the existing authenticated transport:

| Route | Result |
| --- | --- |
| `GET /api/current-plans/schema` | Publication and feedback schemas, body limit |
| `GET /api/current-plans` | Active registrations with current record or null |
| `GET /api/current-plans/{author_id}` | Current record, or 404 if absent/retired |
| `POST /api/current-plans` | Conditional replacement; 201 accepted, 200 retry |
| `POST /api/current-plans/{author_id}/feedback` | Contextual message receipt |

List filters by optional `agent`; `limit` defaults to 50, maximum 100.
`next_after_author_id` is passed as `after_author_id` with the same filter.
Entries sort by author ID. Refresh when the roster changes; pagination is not a
historical snapshot. The page contains `items`, `next_after_author_id`,
`generated_at`, and `stale_after_seconds`. Each entry has `author_id`,
`agent_name` and `record`. A record contains `author_id`, publication-time
`author_name`, current `agent_name`, `plan_id`, `revision_id`, `updated_at`,
`age_seconds`, `stale` and the complete `plan`.

Domain failures use `{detail:{code,message,...}}`. Stale errors include
`current_revision_id` and `current_plan_id`, null before first publication.
Malformed requests return bounded 422 details without echoing rejected content.
Authentication and signing keep their existing error shapes.

The provider retains only the current checklist, not lifetime completed plans.
Retry receipts contain hashes and IDs, not old plan snapshots. They follow
`timing.delivery_retention_days` (30 days by default); retired current records
are removed at normal pruning. Active plans remain until explicitly replaced,
regardless of freshness. Feedback context follows ordinary message retention.
After receipt expiry, an old write cannot replace newer work because its base
revision is stale. Do not infer nondelivery from an expired feedback receipt;
reconcile delivery history before initiating another feedback request.

Requests are at most 65,536 UTF-8 bytes. Plans contain at most 30 items, titles
160 characters, item text 240, detail 400, and three references per item (targets
256 characters). Combined plan text is at most 8,000 characters. Feedback is
2,000 characters with at most ten selected items. Display strings reject controls
and bidi controls; feedback alone permits newline and tab. Back up the whole
database to retain current plans, registration identities and retry receipts.
