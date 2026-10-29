# Current plan — now, next and later

Keep one short agent-authored checklist, separate from progress reports and
runtime approval dialogs. Refresh at meaningful task transitions and daily when
useful; carry unfinished items forward and trim completed work when starting new
work. Backbone does not schedule or execute it.

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
and stable plan/item IDs. Section order is now, next, later; statuses are pending,
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
