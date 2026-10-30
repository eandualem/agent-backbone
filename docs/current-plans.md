# Keep an agent's current plan visible

Use a Current plan to show **one concrete task the agent is working on now** and
the real steps needed to reach its outcome. For example, “Fix export validation”
can include reproducing the failure, implementing the correction, getting the
required review and verifying the result. A list such as “Track exports, receive
analytics, coordinate onboarding” combines unrelated workstreams; it does not
explain how to finish one task. Keep those other commitments in the project's
chosen memory or a tracker.

The agent authors the steps and marks their actual progress. Include investigation,
implementation, review, a person or peer's input, verification and publication
where the task actually needs them. These are examples, not mandatory stages or
new approval requirements. The person reads the plan and can ask questions or
request a change of direction. A Current plan is separate from runtime status,
native plan-approval dialogs, progress reports and longer-lived spreadsheet trackers.

Use a title of a few words and roughly three to seven steps, each two to five
words. Use as many steps as the task needs, rather than padding it to a fixed count.
The `now`, `next` and `later` sections group steps within that same task, not
different tasks or a backlog. Keep `detail` empty or one short clause. For a
`blocked` step, name who or what it waits on and the result needed to proceed:
“Waiting on reviewer for patch review” or “Waiting on you to confirm export scope”.
Waiting is not active execution; set `in_progress` when actually working on the
step. Dependencies are authored explanations, not machine-resolved links: do not
parse names or punctuation in `detail` to infer cross-agent relationships.
Put longer explanations and evidence in a report or conversation.
These are authoring guidelines, not new schema limits.

Refresh at meaningful step changes, when a dependency changes, and daily when
useful. Keep the same `plan_id` while working on the same task, preserving IDs of
continuing steps and showing completed steps as `done`. A different concrete task
gets a new `plan_id` and its own step IDs; do not carry unrelated unfinished items
into it. If you switch tasks while blocked, retain the suspended task, dependency,
`plan_id` and step IDs in the project's chosen memory or a tracker before replacing
the current plan. When returning to that task, reuse its plan and continuing step
IDs, reconcile its steps against current facts, and publish against the latest
base revision. The provider does not retain the replaced checklist. Nothing clears
at midnight, and Backbone does not choose, schedule, execute or complete the work.
An empty item list means no current items; a missing record means the agent has
never published a retained plan. An update at least 24 hours old is marked stale,
not complete.

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
    "title": "Fix export validation",
    "items": [{
      "item_id": "44444444444444444444444444444444",
      "text": "Reproduce the export failure",
      "section": "now",
      "status": "done",
      "detail": "",
      "references": [{"kind": "issue", "target": "example/app#42"}]
    }, {
      "item_id": "55555555555555555555555555555555",
      "text": "Implement the correction",
      "section": "now",
      "status": "done",
      "detail": "Regression checks passed"
    }, {
      "item_id": "66666666666666666666666666666666",
      "text": "Obtain the required review",
      "section": "now",
      "status": "blocked",
      "detail": "Waiting on reviewer for patch review"
    }, {
      "item_id": "77777777777777777777777777777777",
      "text": "Integrate and verify exports",
      "section": "next",
      "status": "pending",
      "detail": "After review findings are resolved"
    }]
  }
}
```

First publication requires `base_revision_id: null`. Later updates replace the
whole short plan atomically and must name its current revision, even when changing
to a new task and `plan_id`. Preserve continuing item IDs within the same task.
Array order is authoritative: `now` items first, then `next`, then
`later`. Statuses are `pending`, `in_progress`, `blocked` and `done`; optional
`detail` names dependencies, peers the item is waiting on, or other useful context.
References use `issue` with `owner/repo#number`, `sheet` with its UUID hex ID, or
`document` with a positive decimal revision ID. References are stored as data, not
fetched or verified.

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
If the sender was renamed and the old name remains unused, the original name
returns `author_unavailable`; changing to the new name under the same key returns
`request_conflict`. If another registration takes the old name, a trusted-client
request can instead resolve to that new sender and queue a new operation: retry
receipts are scoped to the sender registration. After a sender rename or name
reuse, reconcile the sender identity and delivery history before retrying or
deliberately creating a new operation. Changing the key is not a safe automatic
retry. Renaming the recipient preserves its registration-bound
context and does not prevent an unchanged sender from retrying.

For a retained discussion and agent replies, use the shared
[plan conversation](conversations.md#current-plans), keyed by the same `author_id`.
The feedback endpoint above remains available; it does not create a thread entry.

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
Conversation revision/step events retain metadata, and anchored messages retain
only their selected context; they do not retain historical full checklists.
Known replaced version content is explicitly reported as `version_not_retained`.
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
