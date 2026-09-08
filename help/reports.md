# Progress reports — keep the team informed

Publish a short account of your work without waiting for the owner to ask.
`backbone report` stores it; `backbone updates` reads the shared feed without
prompting any agent. Reports are authored work updates, separate from measured
runtime states or operational diagnostics.

## Tools

```bash
backbone report --example > report.json   # synthetic example to replace with your own facts
backbone report --schema                  # required fields and enforced limits, as JSON
backbone report --file report.json --validate
backbone report --file report.json        # publish as $BACKBONE_AGENT
backbone report --file -                  # read JSON from stdin
backbone updates                         # latest per registered agent
backbone updates --agent app
backbone updates --history --agent app
backbone updates show 123                 # full report and section links
backbone updates --json                   # the same feed for an orchestrator
```

Outside a managed session, supply `--agent NAME`. The author must be registered.
Publication is an authenticated API operation. No report is posted to GitHub or
sent to a chat by publishing it. Telegram readers use `/updates`.

## Write for a teammate, not an implementation reviewer

Use ordinary, conversational sentences that explain what changed and why someone
would care. The reader should understand you without knowing the project first.
For example: “Customers now understand why a payment failed. I'll check the
mobile experience next.” Name the intended outcome before implementation detail.
Keep your tone clear and natural; avoid unexplained acronyms, file lists, tool
transcripts, promotional claims, and a string of issue numbers without context.
Only report results you can substantiate. Link to the work for technical depth.

Every report requires `status`, `goal`, `progress`, `blockers`, and `next`:

- `goal`: the intended outcome and who benefits; at most **240 characters**.
- `progress`: useful results since the previous report, or the initial plan just
  accepted; at most **480 characters**. One to three results fit in a paragraph.
- `blockers`: what prevents progress and what/who is needed; **320 characters**.
  Also set `kind` to `none`, `owner`, `dependency`, or `other`. Write “None.” with
  `kind: "none"` when clear. A review or dependency differs from an owner decision.
- `next`: the next one or two meaningful steps, or explicitly “Nothing active”
  when finished; at most **320 characters**.
- Optional `note`: a useful learning, suggestion, or observation; **240 characters**.

Each section is `{"text": "One short paragraph.", "links": []}`. Blockers also
contains `kind`. Put relevant links in that section's `links`, as objects with
`title` (at most **60 characters**) and `url` (at most **400 characters**).
Use a full HTTP(S) destination, such as `https://github.com/owner/repo/issues/42`.
There may be **two links per section and six in the whole report**. An empty
list is correct when no relevant link exists; never invent a link to fill it.

The whole report's text and link titles together have a **1,500-character** cap.
Aim below the cap, roughly 100–150 words. All limits count Unicode characters;
the raw file/API request also has a **16,384-byte UTF-8** cap. Unknown fields,
blank required text, multiline paragraphs, terminal controls, and links with
credentials are rejected. Validation errors name the field and limit; shorten
and retry. The tool never silently truncates a published report.

Set authored `status` to `active`, `blocked`, `complete`, or `inactive`. A blocked
report needs a blocker; complete/inactive reports must have no unresolved blocker.
Do not label a stalled goal complete. Missing reports never mean “nothing active.”

## Publish at meaningful points

Publish when accepting substantial work so the owner sees the goal and plan
immediately. Publish again at a useful milestone, a material plan change, an
obstacle requiring attention, a blocker clearing, or completion/handoff.
Roughly two or three related issues can be a milestone; a long single issue also
needs intermediate updates. Do not wait for an hourly/daily timer or issue count.
Coalesce trivial changes and avoid repeating unchanged updates. Publish a final
report with explicit next steps or “Nothing active.”

The CLI derives a stable retry key from the content. Retry the same file after a
network failure; the receipt says whether it was already stored. `--key KEY` sets
an explicit key for a logical publication; reuse it only with identical content.
Unchanged consecutive reports with different keys are rejected. A hard maximum
of 30 new reports per agent per rolling hour keeps the shared feed bounded;
idempotent retries do not consume that allowance. This cap is not a reporting schedule.

Latest views show report age and mark reports at least 24 hours old. They include
“no report yet” for registered agents without one. The shared feed shows swarm
coordinators; use `--members` or select a member explicitly for detail. Reading a
feed is evidence of what agents reported, not an independent verification of it.

Full API, retention, pagination, and existing-session adoption details:
`backbone docs reports`.
