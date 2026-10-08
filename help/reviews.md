# Run a deep review while continuing your work

Use `backbone docs deep-reviews` for the complete, tested native CLI workflow.
Any repository agent can invoke it through its shell tool; the caller does not
need to use the same runtime as the reviewer. No managed agent or swarm is created.

1. Commit the intended changes, fetch refs, and pin the head, base and merge-base.
2. Review the head in a separate clean checkout. For a feature branch, head is
   the branch and base is develop; fix valid findings and re-run checks before
   opening its PR, then follow the guide's "Before and after the pull request".
   For develop into main, head is develop and base is main. Preserve the
   implementing agent's active checkout.
3. Use the reviewer for the CLI that wrote the change. On the Codex CLI, use
   Claude Opus 5.5 (`claude-opus-5-5`), preferably, or Codex `gpt-6-astra`; on
   Claude Code or any other runtime, use Codex `gpt-6-astra`. The core of each
   command is `codex exec review --base BASE_COMMIT --model gpt-6-astra` and
   `claude -p "REVIEW PROMPT" --model claude-opus-5-5 --effort high`; run the full
   commands from the guide (its "Codex reviewer" and "Claude Opus reviewer"),
   which add the read-only settings, the depth and the evidence files.
   Start a separate, fresh reviewer context; the implementing conversation reviewing itself does not
   satisfy the independent-review gate. A different family or CLI is not required.
   Run with the reviewer's read-only settings and hooks disabled, in an environment
   without the caller's Backbone identity, saving JSON evidence as the guide shows.
   For Codex, pin the standard service tier explicitly, including with
   `--ignore-user-config`; it is separate from reasoning effort.
   Depth: `high` for a feature branch, **before** its PR opens. An explicitly
   authorized develop → main release review uses Codex `gpt-6-astra` at `ultra`,
   whatever CLI wrote the change, until an Opus release depth is set. Record the
   model the reviewer reports using (Opus: `modelUsage`), not only the one you
   asked for. If no permitted reviewer can run, report the
   blocker rather than substituting another model.
4. Start the process in the background, retain its handle, and continue working.
   Capture exit status; a timeout, failed launch or missing report is not a clean
   review. Keep artifacts in ignored `.backbone/reviews/`, outside `docs/`.
5. Triage every finding against the reviewed head: valid or rejected, with severity
   and a reason. Fix valid findings on a branch, never on the base. Send fixes as
   a PR to `develop`, address automated review and pass required checks, then merge
   when clear. Create issues for findings only when authorized.
6. Decide from the triaged findings whether another Ultra round is warranted.
   Many valid findings, or any high-severity ones, suggest the pass saturated and
   more may remain: run another head → base review after fixes land, with the
   same reviewer at release depth (`gpt-6-astra`, `ultra`). Few,
   minor findings mean fix them through the ordinary PR/review/check/merge path
   and **do not run another Ultra pass**. Zero valid findings also ends the rounds.
   Apply this judgment after every round; use neither a fixed pass count nor an
   iterate-until-clean loop. Account for changes after the reviewed snapshot.
7. In each round's progress report, state finding counts by severity (including
   rejected findings), what was fixed with PR links, the continue/stop decision
   and why, elapsed time and token usage (`backbone usage`, scoped to the review
   session when available). State missing attribution explicitly; never assign
   unrelated concurrent usage to the round. Keep detailed evidence in the run
   directory and the published report within `backbone help reports` limits.
8. **State the stop.** For a develop → main release review, after the final few,
   minor findings are fixed and merged (or there are no valid findings), open the
   release PR from `develop` to `main`, or merge it when authorized and checks and
   review are clear. Verify the PR's state and any completed issue's closure,
   report completion or the concrete pending gate. A review alone does not
   authorize a release; finish within the scope agreed with the operator.

Read the guide before first use, especially its sandbox initialization guidance.
Never stop the caller's session or bypass the reviewer's sandbox to launch a review.
Codex `ultra` is a reasoning-effort setting in this workflow, not a separate
cloud review service.
