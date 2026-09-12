# Run a deep review while continuing your work

Use `backbone docs deep-reviews` for the complete, tested native CLI workflow.
Any repository agent can invoke it through its shell tool; the caller does not
need to use the same runtime as the reviewer. No managed agent or swarm is created.

1. Commit the intended changes, fetch refs, and pin the head, base and merge-base.
2. Review the head in a separate clean checkout. For develop into main, head is
   develop and base is main. Preserve the implementing agent's active checkout.
3. Run Codex's `exec review --base BASE_COMMIT` with explicit `gpt-6-astra` and
   `model_reasoning_effort=ultra`, the read-only sandbox, hooks disabled and a
   fresh environment without the caller's Backbone identity. Use `--json` and
   `--output-last-message` to save execution evidence and a readable final report.
4. Start the process in the background, retain its handle, and continue working.
   Capture exit status; a timeout, failed launch or missing report is not a clean
   review. Keep artifacts in ignored `.backbone/reviews/`, outside `docs/`.
5. Triage every finding against the reviewed head: valid or rejected, with severity
   and a reason. Fix valid findings on a branch, never on the base. Send fixes as
   a PR to `develop`, address automated review and pass required checks, then merge
   when clear. Create issues for findings only when authorized.
6. Decide from the triaged findings whether another Ultra round is warranted.
   Many valid findings, or any high-severity ones, suggest the pass saturated and
   more may remain: run another head → base Ultra review after fixes land. Few,
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
   report completion or the concrete pending gate, and start nothing else. A
   person's request to “run an Ultra review” means this findings-based sequence.

Read the guide before first use, especially its sandbox initialization guidance.
Never stop the caller's session or bypass the reviewer's sandbox to launch a review.
Claude Code's native `ultrareview` is a distinct cloud service, not an interchangeable
name for Codex Ultra effort. Its account access must be checked separately.
