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

Verify each finding with code or a regression test. Create issues for actionable
findings when authorized, then fix them through ordinary reviewed PRs. Recheck the
final release diff after fixes; do not treat an older review as covering new code.
Publish a short progress report with links to issues/PRs, not the raw model log.

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
