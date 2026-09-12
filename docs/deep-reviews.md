# Deep reviews without stopping your agent

A repository agent can run the installed Codex CLI as a separate process while
its own conversation continues. No Backbone swarm or managed agent session is
needed. Use a deep review before a release or a broad architectural change;
use a focused review for a small change and a swarm for independent implementation
work. The review produces findings for an agent to verify, not automatic fixes.

## Codex: explicit Ultra effort

Check `codex --version` and `codex exec review --help`. A complete review was verified with CLI
0.153.4, with `gpt-6-astra` supporting `ultra`. Ultra is a reasoning setting for
Codex's review command, not a separate `codex ultrareview` subcommand. Set both
model and effort explicitly rather than inheriting the implementing agent's defaults.

Fetch the intended repository refs, resolve the head and base to commit IDs, and
use a clean, detached checkout of the head. For develop into main, the reviewed
head is develop and the comparison base is main. This is a merge-base diff;
record the merge-base too. Never switch an agent's active checkout underneath it.

For example, run these Bash/Zsh commands from your repository. They select
committed refs, so commit intended local changes first. Use a new run directory
for every attempt; never overwrite an earlier review's evidence.

```bash
git fetch origin
review_head=$(git rev-parse --verify 'origin/develop^{commit}')
review_base=$(git rev-parse --verify 'origin/main^{commit}')
review_run="$PWD/.backbone/reviews/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$review_run"
git worktree add --detach "$review_run/repo" "$review_head"
printf '%s\n' "head=$review_head" "base=$review_base" \
  "merge_base=$(git merge-base "$review_base" "$review_head")" \
  "model=gpt-6-astra" "effort=ultra" > "$review_run/scope.txt"
codex --version >> "$review_run/scope.txt"
```

The essential invocation, from that clean checkout, is below. Replace
`BASE_COMMIT` with the saved base SHA and the output paths with your run directory.
Ask your agent to execute it in the background; you do not need to open or stop
an interactive Codex session.

```bash
env -u BACKBONE_AGENT -u BACKBONE_RUNTIME -u BACKBONE_STATE_DIR \
  -u BACKBONE_DATA_DIR -u BACKBONE_API_KEY -u CODEX_THREAD_ID \
  codex exec --sandbox read-only --ignore-user-config --ephemeral \
  --disable hooks --disable shell_snapshot \
  -c approval_policy=never \
  -c model_reasoning_effort=ultra \
  review --base BASE_COMMIT --model gpt-6-astra \
  --json --output-last-message /ABSOLUTE/RUN/DIR/report.md \
  > /ABSOLUTE/RUN/DIR/events.jsonl 2> /ABSOLUTE/RUN/DIR/stderr.log
```

Use a fresh environment without the implementing agent's `BACKBONE_*` identity,
state paths or secrets. Disabling hooks prevents reviewer events from overwriting
the caller's state or acknowledgments. `--ignore-user-config` leaves Codex's own
login available while skipping its user configuration file. Repository instructions still apply.
The review retains its read-only sandbox; do not bypass it to remove a prompt.

Run through the caller's background command facility and retain the process handle.
For a job that must outlive that facility, use a detached supervisor with closed
stdin that records the process exit code and timestamps. Save the command, CLI
version, model, effort, head, base and merge-base alongside the report. Keep these
artifacts under the repository's ignored `.backbone/reviews/` directory, not `docs/`.

If the launching agent reports `failed to initialize in-process app-server client:
Operation not permitted`, Codex may be blocked by the outer agent sandbox before
it can start. Request permission for this specific review process to initialize
outside that outer sandbox, retaining the inner `--sandbox read-only` and disabled
hooks. Do not use `--dangerously-bypass-approvals-and-sandbox`. A missing model or
authentication failure is a different problem: inspect stderr and preserve the
requested model rather than silently substituting another one.

## Retrieve and assess the result

Read the saved final report after the process completes. Retain the event log and
stderr for diagnosing failures. An absent or empty final report, nonzero exit,
interruption, timeout, authentication error or unsupported model is a failed review,
not a clean review. Findings must be checked against the recorded head. If either
branch advances, compare its new commit before using the review as a release gate.

Triage every finding against code or a regression test: record whether it is valid
or rejected, its severity and the reason. Create issues for actionable findings
only when authorized. Fix valid findings on a branch, **never on the base**. Open
a PR to `develop`, address automated review and pass required checks, then merge
when clear. Recheck the final release diff after fixes; do not treat an older
review as covering new code.

## Decide whether to run another Ultra round

A person's request to “run an Ultra review” means a findings-based sequence, not
one pass by definition and not repeated passes until a clean report. Decide after
each completed round, using its triaged findings:

- **Many valid findings, or any high-severity findings:** assume the pass may have
  saturated and left other problems unreported. After the fixes have landed in
  `develop` through ordinary reviewed PRs, run another Ultra pass from the updated
  head to the intended base. Pin the new head, base and merge-base and use a new
  run directory. Apply the same decision rule to that round's findings.
- **Few findings, all minor:** fix them on a branch, PR to `develop`, complete
  ordinary automated review and required checks, and merge. Then stop the Ultra
  rounds; do **not** run another Ultra pass merely to obtain a zero-finding report.
- **No valid findings:** stop the rounds without manufacturing a fix PR.

“Many” and “few” require judgment about the scope and significance of the findings;
they are not numeric thresholds or a fixed round budget. Explain ambiguous cases
and why the findings justify continuing or stopping. Changes made after the
reviewed snapshot still need review; small fixes in the final minor round receive
ordinary PR review, rather than automatically restarting the Ultra sequence.

### Make each round auditable

Publish a short progress report after every round with:

- The round and reviewed head/base, plus finding counts by severity. Distinguish
  valid findings from rejected ones so raw model output does not determine the
  next round.
- What was fixed and links to the fix PRs, including their landing status.
- The decision to continue or stop and the findings that justify it.
- The round's elapsed time and tokens, with their source and attribution limits.

Use `backbone usage` and, when the review has an attributable ledger session,
`backbone usage session ID` for token details. Retain the review process's start/end
times and usage events with its saved artifacts. A detached ephemeral reviewer may
not have a Backbone ledger entry: use its recorded usage when available and state
that source; otherwise report tokens as unavailable. Never report missing usage
as zero or attribute a fleet-wide delta to one round while other agents are working.
Keep the published report within `backbone help reports` limits and link to detailed
evidence rather than pasting the model log. The per-round reports must make both
the resource cost and the reason for another pass visible to a human.

### The stop is part of the task

For a requested develop → main release review, once the final round has few, minor
findings and its fixes have landed (or no valid findings), open the release PR from
`develop` to `main`. Merge when authorized, after the release PR's required checks
and review are clear. Verify the PR state and closure of fully completed issues;
do not describe an open PR or unresolved gate as finished delivery. If the original
request already authorizes merging, carry it through rather than seeking approval
again. State the stop in the final report and start nothing else.

For a review whose scope does not include a release, finish the authorized fix PRs
and report the stop at that boundary. The convergence rule does not authorize an
unrequested release or additional implementation work.

## Claude Code

`claude ultrareview BASE_BRANCH --no-post` is a separate, cloud-hosted review
capability. `--json` returns findings, and `--timeout MINUTES` bounds how long the
command waits. It is not ordinary Claude `--effort max`. Check local help and account
availability first. Backbone does not silently replace the requested Codex review
with this service. No Claude review was executed for the Codex verification.

The full tracked review-job API, cancellation and automatic completion notices
are follow-up work in [issue #171](https://github.com/eandualem/agent-backbone/issues/171); this guide describes the native CLI workflow.

References: [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
and [Code review](https://learn.chatgpt.com/docs/code-review).
