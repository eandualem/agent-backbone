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
5. Verify findings, publish a short progress report, and use ordinary issues/PRs
   for fixes when authorized. Check the reviewed head against the intended release
   before merging; review changes made after that snapshot.

Read the guide before first use, especially its sandbox initialization guidance.
Never stop the caller's session or bypass the reviewer's sandbox to launch a review.
Claude Code's native `ultrareview` is a distinct cloud service, not an interchangeable
name for Codex Ultra effort. Its account access must be checked separately.
