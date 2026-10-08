# Deep reviews without stopping your agent

A repository agent can run a reviewer CLI as a separate process while its own
conversation continues. No Backbone swarm or managed
agent session is needed. The review produces findings for an agent to verify,
not automatic fixes.

The reviewer depends on the CLI that wrote the change. An agent on the Codex
CLI uses Claude Opus 5.5 (`claude-opus-5-5`), preferably, or Codex
`gpt-6-astra`. Every other agent, Claude Code included, uses Codex
`gpt-6-astra`. Independence means a separate, fresh reviewer context reviewing
the pinned changes, not the implementing conversation checking itself. A
different model family or CLI is not required. Name the model exactly; if no
permitted reviewer can run, report that rather than substituting another one.

| Scope | Head | Base | Reviewer and depth |
|---|---|---|---|
| Feature branch, **before** its PR opens | the branch's commit | the integration branch (`develop`) | Codex implementer: [Opus](#claude-opus-reviewer-for-codex-implementers) at `--effort high`, or Astra at `high`. Any other: [Astra](#codex-reviewer) at `high` |
| Release, develop into main | `develop` | `main` | [Astra](#codex-reviewer) at `ultra`, whatever wrote the change |

Use `high` for ordinary feature work. A release review at `ultra` still requires
explicit release authorization.

## Codex reviewer

Check `codex --version` and `codex exec review --help`. Complete reviews were verified
with `gpt-6-astra` at `ultra` (CLI 0.153.4) and at `high` (CLI 0.155.1). An unknown
model fails: exit 1, a `turn.failed` event and no report. Ultra is a reasoning setting for
Codex's review command, not a separate `codex ultrareview` subcommand. Set
model, effort and `service_tier=default` explicitly. The standard service tier is
independent of reasoning effort, including `ultra`; it does not select Fast or
Ultrafast processing.

Fetch the intended repository refs, resolve the head and base to commit IDs, and
use a clean, detached checkout of the head. This is a merge-base diff;
record the merge-base too. Never switch an agent's active checkout underneath it.

For example, run these Bash/Zsh commands from your repository. They select
committed refs, so commit intended local changes first. Use a new run directory
for every attempt; never overwrite an earlier review's evidence.

```bash
git fetch origin
# Feature branch before its PR: the branch you committed on, against develop.
review_head=$(git rev-parse --verify 'HEAD^{commit}')
review_base=$(git rev-parse --verify 'origin/develop^{commit}')
review_effort=high
# Release review instead: head origin/develop, base origin/main, review_effort=ultra.
review_run="$PWD/.backbone/reviews/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$review_run"
git worktree add --detach "$review_run/repo" "$review_head"
printf '%s\n' "head=$review_head" "base=$review_base" \
  "merge_base=$(git merge-base "$review_base" "$review_head")" \
  "model=gpt-6-astra" "effort=$review_effort" "service_tier=default" > "$review_run/scope.txt"
codex --version >> "$review_run/scope.txt"
```

The essential invocation, from that clean checkout, is below. Replace
`BASE_COMMIT` with the saved base SHA, `EFFORT` with `high` or `ultra` from the
table, and the output paths with your run directory.
Ask your agent to execute it in the background; you do not need to open or stop
an interactive Codex session.

```bash
env -u BACKBONE_AGENT -u BACKBONE_RUNTIME -u BACKBONE_STATE_DIR \
  -u BACKBONE_DATA_DIR -u BACKBONE_API_KEY -u CODEX_THREAD_ID \
  codex exec --sandbox read-only --ignore-user-config --ephemeral \
  --disable hooks --disable shell_snapshot \
  -c approval_policy=never \
  -c model_reasoning_effort=EFFORT \
  -c service_tier=default \
  review --base BASE_COMMIT --model gpt-6-astra \
  --json --output-last-message /ABSOLUTE/RUN/DIR/report.md \
  > /ABSOLUTE/RUN/DIR/events.jsonl 2> /ABSOLUTE/RUN/DIR/stderr.log
```

Use a fresh environment without the implementing agent's `BACKBONE_*` identity,
state paths or secrets. Disabling hooks prevents reviewer events from overwriting
the caller's state or acknowledgments. `--ignore-user-config` leaves Codex's own
login available while skipping its user configuration file, including any service-tier
default there. Keep the explicit `service_tier=default` override even when that
file already selects the standard tier. Repository instructions still apply.
The review retains its read-only sandbox; do not bypass it to remove a prompt.

Run through the caller's background command facility and retain the process handle.
For a job that must outlive that facility, use a detached supervisor with closed
stdin that records the process exit code and timestamps. Save the command, CLI
version, model, effort, service tier, head, base and merge-base alongside the
report. Keep these artifacts under the repository's ignored `.backbone/reviews/` directory, not `docs/`.
CLI 0.155.1 reports every token count as 0 in `exec review`'s `turn.completed`
event; record tokens as unavailable, not zero. Remove the detached checkout afterwards with `git worktree remove "$review_run/repo"`;
the saved evidence stays in the run directory.

If the launching agent reports `failed to initialize in-process app-server client:
Operation not permitted`, Codex may be blocked by the outer agent sandbox before
it can start. Request permission for this specific review process to initialize
outside that outer sandbox, retaining the inner `--sandbox read-only` and disabled
hooks. Do not use `--dangerously-bypass-approvals-and-sandbox`. A missing model or
authentication failure is a different problem: inspect stderr and preserve the
requested model rather than silently substituting another one.

## Claude Opus reviewer (for Codex implementers)

An agent working in the Codex CLI reviews its feature branch with Claude Code
running `claude-opus-5-5`, the same way: a separate headless process in the
clean detached checkout, while the calling agent keeps working. Prepare the run
directory, checkout and `scope.txt` as above, recording `model=claude-opus-5-5`
and `effort=high`, and save `claude --version` in place of Codex's. Measured
with Claude Code 2.1.294.

```bash
cd /ABSOLUTE/RUN/DIR/repo
env -u BACKBONE_AGENT -u BACKBONE_RUNTIME -u BACKBONE_STATE_DIR \
  -u BACKBONE_DATA_DIR -u BACKBONE_API_KEY -u BACKBONE_LAUNCH_ID \
  claude -p "Review the changes from BASE_COMMIT to HEAD in this repository: \
read them with git diff BASE_COMMIT...HEAD and git log BASE_COMMIT..HEAD, and \
read the surrounding code. Report only problems a maintainer would fix (bugs, \
regressions, security issues), each checked against the code. Do not change \
any file. Reply with a JSON array of findings, each with file, line, severity \
(P0 to P3), summary and failure_scenario, then one short paragraph; reply [] \
if you find nothing." \
  --model claude-opus-5-5 --effort high \
  --permission-mode dontAsk --tools "Read,Grep,Glob,Bash" \
  --allowedTools "Read Grep Glob Bash(git diff:*) Bash(git log:*) Bash(git show:*)" \
  --no-session-persistence --settings '{"disableAllHooks":true}' \
  --output-format json \
  < /dev/null > /ABSOLUTE/RUN/DIR/report.json 2> /ABSOLUTE/RUN/DIR/stderr.log
```

`--tools` leaves the reviewer only reading tools and Bash, and `dontAsk` with
`--allowedTools` refuses every command other than `git diff`, `git log` and
`git show` without asking; refusals are listed in `permission_denials`. This is
Claude Code's permission check, not an operating-system sandbox like Codex's
`--sandbox read-only`, so run it only in the detached review checkout.
`disableAllHooks` skips every hook in user and project settings, including ones
Backbone did not install, and `--no-session-persistence` leaves no resumable
conversation. Do not use `--bare` for isolation: it refuses OAuth and keychain
logins, so a subscription login cannot authenticate.

Use a plain review prompt like the one above, not Claude Code's built-in
`/code-review`: that skill chooses its own model, so `--model` does not decide
who reviews (measured: a run asked for `haiku` was answered by
`claude-sonnet-5-5`).

**Exit status 0 does not prove a review ran.** Count the review as done only when
`modelUsage` names the requested model and `result` holds the findings:

```bash
jq -e '(.is_error | not) and (.modelUsage | has("claude-opus-5-5"))' \
  /ABSOLUTE/RUN/DIR/report.json
```

Save the model from `modelUsage`, `total_cost_usd` and `usage` with the report;
they are the round's model, cost and token evidence.

From inside a Codex session the reviewer needs network access to Anthropic's
API. An agent Backbone launched on Codex has it (`claude -p --model
claude-opus-5-5` was measured to run there). Elsewhere, ask for this one process
to run outside the outer sandbox and keep the permission settings above.

Release reviews use Astra at `ultra` for every implementer, as described in
[Codex reviewer](#codex-reviewer).

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

For an iterative review, decide whether another round is useful from the
triaged findings and the authorized scope:

- **Many valid findings, or any high-severity findings:** assume the pass may have
  saturated and left other problems unreported. After the fixes have landed in
  `develop` through ordinary reviewed PRs, run another release-depth pass with the
  same reviewer (`gpt-6-astra` at `ultra`) from the updated
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

### Finish within the review scope

Report the reviewed commits, remaining findings and whether fixes have landed.
A review does not itself authorize a release. When release work is in scope,
follow the repository's [promotion process](../CONTRIBUTING.md#releasing-maintainers)
and verify the resulting PR state before reporting completion.

## Historical review measurements

These measurements describe earlier reviewer configurations, not approved
reviewer selections. Use the reviewers named at the top of this guide; do not
launch the earlier `/code-review` recipe.

On a one-file diff with two seeded bugs, Claude `claude-fable-5-1` at `high`
took 99 s and $0.56 and found both. The same diff at `max` (measured with Opus)
took 850 s and $6.08. These observations are not Astra timing or price estimates.
Record elapsed time and attributable usage for each actual review as described
above. Keep `ultra` for authorized release reviews, where
[another round](#decide-whether-to-run-another-ultra-round) is decided from the
findings.

## Before and after the pull request

For a feature branch, finish the review before opening the PR. Triage every
finding against the reviewed head as described above, fix the valid ones on the
branch and run the repository's checks again. A meaningful change made after the
review (new behaviour, not a typo) gets another `high` review of the new head. Then
open the PR against `develop` and link the saved report.

After the PR opens, required checks and branch protection still gate the merge.
Whether CodeRabbit is also required (public repositories) or not (private ones)
is set by the shared `independent-review` policy (`backbone templates show
independent-review`). When it is required, confirm that a review ran on the latest commit:
the check reads "Review completed", not "Review rate limited" or "Review skipped".
If none started, comment `@coderabbitai review` on the PR. When it is rate limited,
send that comment only after the "available in N minutes" time it gives. A GitHub
Actions payment does not enable CodeRabbit; its private-repository reviews are a
separate subscription.

The full tracked review-job API, cancellation and automatic completion notices
are follow-up work in [issue #171](https://github.com/eandualem/agent-backbone/issues/171); this guide describes the native CLI workflow.

References: [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
and [Code review](https://learn.chatgpt.com/docs/code-review).
