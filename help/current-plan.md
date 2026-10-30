# Current plan — steps for one concrete task

Show the one concrete task you are currently working on and its real steps toward
the outcome. “Fix export validation” might need reproduction, implementation,
review and verification. Use the stages the task needs; do not invent approval
gates or fill the plan with unrelated workstreams. A plan is separate from a
backlog, progress report or runtime approval dialog.

Use `now`, `next` and `later` for steps of that same task. A blocked step's `detail`
must say who or what it waits on and what is needed, for example “Waiting on
reviewer for patch review”. Do not mark waiting as active work or parse this text
as a structured dependency. Read `backbone docs current-plans` for a full example.

Update at meaningful step or dependency changes and daily when useful. Keep the
same plan and continuing step IDs for the same task; mark finished steps `done`.
Start a new `plan_id` and new step IDs for a different task. Preserve a suspended
task elsewhere before replacing its checklist: only the current plan is retained.
Backbone does not choose, schedule or execute the work.

```bash
backbone current-plan --agent NAME --json
backbone current-plan publish --schema
backbone current-plan publish --file plan.json --validate
backbone current-plan publish --file plan.json --json
backbone current-plan show AUTHOR_ID --json
backbone current-plan feedback AUTHOR_ID --schema
backbone current-plan feedback AUTHOR_ID --file question.json --json
backbone docs current-plans
```

Discover the stable author ID first. Publish a small full snapshot with a fresh
request ID, the current base revision (null only before the first publication),
and plan/item IDs for the current task. A new task still uses the latest base
revision; it does not reset that field to null. Section order is now, next, later; statuses are pending,
in_progress, blocked and done. Completion is authored by the agent. A person asks
or requests reprioritization through feedback, tied to the observed plan/revision
and selected item IDs.

A stale edit or feedback returns 409: preserve the draft, read current state and
explicitly reconcile. An uncertain transport result is not failure proof; retry
identical content with the same request ID. Existing signing requirements apply.
Agent credentials cannot send operator feedback. A queue receipt is not an answer.

Only current content is retained; retry metadata follows ordinary delivery
retention. An empty checklist means no current items; an absent plan means not
published; an age of at least 24 hours is stale. Nothing is automatically cleared
at midnight. `--schema` and `--validate` are local and do not publish.
