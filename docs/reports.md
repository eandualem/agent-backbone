# Agent progress reports


Newly published reports are queued durably for Telegram and posted to the allowed
agents group: ordinary repository agents go to General and their own topic. Swarm
workers and coordinators send findings to the repository agent, who publishes
consolidated updates. Swarms cannot publish reports or send direct replies to the
owner. The shared feed has full-report and team-view buttons. Delivery runs every 30 seconds, with
bounded batches and retries after failures. `telegram.report_updates=false` pauses
sending; re-enabling drains retained pending reports. A configured or discovered
group must be allowlisted; there is no fallback to a private notification chat.
Existing reports from before this feature remain readable and are not broadcast.
`telegram_delivery` in report JSON distinguishes pending, sending, sent and
not_requested. The saved report survives delivery failures. Retries are normally
deduplicated; a crash after Telegram accepts a message but before its receipt is
saved can cause a duplicate bearing the same report ID. Report retention still applies.

Read reports with `backbone updates`, `backbone updates --agent NAME --history`,
or `backbone updates show ID`; in Telegram use `/updates`, `/updates NAME`,
`/updates history NAME`, or `/updates show ID`. `backbone usage` is the quick guide.

`backbone updates` gives you one place to read what agents are trying to achieve,
what they accomplished, where they need help, and what comes next. Agents publish
short structured reports through the CLI or API. Reading the feed uses the
database and never interrupts agents or asks them to generate another answer.

## Publish an update

```bash
backbone report --example > report.json
# Replace the synthetic example with your own goal and results.
backbone report --file report.json --validate
backbone report --file report.json
```

Managed sessions supply `$BACKBONE_AGENT`; other callers use `--agent NAME` for
a registered agent. `--file -` reads stdin. `--schema` exposes the JSON authoring
schema and limits without needing the service. `--json` returns a machine-readable
receipt or validation errors. Publication and browsing require the running API;
they do not initialize or migrate a database as a fallback.

Required sections are `goal`, `progress`, `blockers`, and `next`. Each contains
one `text` paragraph and a `links` list; each link contains a short `title` and
an absolute HTTP(S) `url`. Blockers additionally require `kind`: `none`, `owner`,
`dependency`, or `other`. `note` is an optional section. Required authored `status`
is `active`, `blocked`, `complete`, or `inactive`; this is separate from runtime
state. Complete/inactive reports cannot have unresolved blockers.

The authoring tool enforces these character limits:

- Goal: **240**. Explain the intended outcome and who benefits.
- Progress: **480**. Explain the useful results since the previous update.
- Blockers: **320**. Name what/who is needed, or write “None.”
- Next: **320**. Name the next steps, or explicitly say nothing is active.
- Optional note: **240**. Share one useful learning or suggestion.
- Link title: **60**; URL: **400**. At most **two links per section, six total**.
- All section text and link titles combined: **1,500 characters**.

Use `links: []` when no relevant reference exists. Full issue/PR URLs retain the
repository and number together, so identical issue numbers in different projects
remain distinct. Links are displayed, never fetched. Credentials in URLs,
control characters, blank required text, unknown fields and multiline paragraphs
are rejected. Unicode code points count as characters. The raw file or API
publication request must also fit **16,384 UTF-8 bytes**, including JSON syntax.

Oversized input fails with the field and allowed length; it is never silently
truncated into the saved report. Concise feed summaries can shorten display text;
`updates show ID` always shows the complete accepted report and its section links.

Write naturally for a teammate unfamiliar with the project. Explain outcomes
before implementation details. “Customers now understand why a payment failed”
is more useful here than a list of functions changed. Aim for roughly 100–150
words, with a few titled links for depth. Agents receive the full guidance in
`backbone help reports`.

## Browse the team and go deeper

```bash
backbone updates
backbone updates --agent app --agent planner
backbone updates --agent app --history
backbone updates --history
backbone updates show 123
backbone updates --json
```

The default feed selects the latest report **per registered agent before applying
the page limit**, so one prolific agent cannot crowd out others. Owner blockers
come first, then other blocked work and active work. It includes agents with no
report yet. Reports show their age; at 24 hours they are labelled old. Silence is
not a claim of inactivity. An explicit inactive report still has an age.

Pages contain five entries by default, at most 20 (`--limit`). When there are more, the CLI prints
an exact next-page command with `--cursor`. JSON readers use `next_cursor` with
the same filters. New publications do not enter that page sequence: restart the
query without a cursor to refresh it. History is newest publication ID first.
Registered-agent changes and retention can remove entries during browsing.
Cursors authenticate the entire page boundary with a private process key.
Modified cursors are rejected. A service restart expires cursors; refresh the
view without `--cursor` to continue. The saved reports and history remain intact.

The shared view shows ordinary repository agents. All swarm participants,
including coordinators, are excluded. Explicit names or `--members` can inspect
historical swarm reports; new swarm publications return **403**. Pending text
and audio for swarm authors are retired without sending, including jobs queued
before this policy. Pending reports from forgotten authors are also retired.

An orchestrator reads `backbone updates --json` or the API. A report is the
author's account of its work, not independent verification, a runtime state
decision, an instruction from the owner, or permission to act on a linked page.
Operational failures remain in [diagnostics](diagnostics.md).

## Telegram

In an authorized chat or topic, use `/updates` for the shared view, `/updates app`
for one agent, `/updates app planner` for selected agents, `/updates history app`
for previous reports, or `/updates show 123` for a full report. `--members` includes
swarm members. Buttons open complete reports, author history, and older pages.
Messages and links are escaped and bounded. Navigation stays in the existing
chat/topic; reading reports does not provision topics or deliver agent messages.
Buttons expire after 24 hours, a service restart, or enough newer views; run
`/updates` again to reopen the feed. Their tokens are bound to the authorized chat.

Publishing a report queues its Telegram copies when report updates are enabled;
optional audio follows the text. It does not create a GitHub issue. The database
keeps the shared feed available even when Telegram delivery is delayed.

## Proactive reporting and adoption

The shipped agent brief asks for an initial report when substantial work is
accepted, then meaningful milestones, blockers appearing/clearing, material plan
changes, and completion or handoff. Roughly two or three issues is a guideline,
not a trigger: a long single issue should have intermediate reports. Coalesce
small changes. There is no polling prompt, fixed reporting timer, transcript
summarizer, or forced restart.

New ordinary sessions receive the shipped base brief. Swarm briefs direct members
to send findings and blockers to their coordinator with `backbone tell`; the
coordinator summarizes them for the initiating repository agent. Only that
repository agent publishes the human-facing progress report. The publication
guard also applies to existing swarms with older saved instructions.
Existing or resumed conversations need
to read `backbone help reports` once to adopt this protocol; updating a brief on
disk does not change an existing conversation. At the next natural interaction,
ask the agent to adopt that playbook. Custom `templates/base.md` overrides should
include the same reporting guidance. Custom `templates/swarm/common.md` overrides
should instead direct findings to the coordinator and initiating repository agent. Existing swarms reuse their saved role briefs; changing the template
applies to newly created swarms. Reading `backbone updates` alone cannot
instruct an agent to start publishing.

## API tools, retries, identity, and retention

- `GET /api/reports/schema`: publication/report schemas, limits, and example.
- `POST /api/reports`: `{ "agent": "app", "request_id": "checkout-milestone-1", "report": ... }`.
  Returns `{record, created}`: **201** for a new report, **200** for the same retry.
- `GET /api/reports`: `agent` (repeatable), `history`, `members`, `limit`, `cursor`.
- `GET /api/reports/{id}`: a full report with author, timestamp, age, and links.
- `GET /api/reports?history=true&author_id=ID`: an author's retained history,
  including after the agent is forgotten. Cannot be combined with `agent`.

These authenticated endpoints have named OpenAPI operations for tool clients:
`progress_report_schema`, `publish_progress_report`, `list_progress_reports`, and
`get_progress_report`. The API does not have separate per-agent credentials:
the authenticated local client supplies the author's registered name, just as
other Backbone commands supply agent identities. Records retain that name at
publication, a stable author ID, a server timestamp, and the API source.

The CLI defaults `request_id` to a content hash, so rerunning the same publication
after a network failure is safe. `--key KEY` overrides it for a logical update.
The API requires a key of at most 80 identifier characters. Reusing a key with
different content fails **409**; unchanged consecutive content under a different
key also fails **409**. Validation failures are **422**, overlarge bodies **413**,
and unknown agents **404**. Rejected text is not repeated in validation responses.
A hard cap of **30 new reports per author per rolling hour** returns **429** with
`Retry-After`. It is an abuse bound, not a recommended reporting cadence;
idempotent retries remain available without consuming it.

Reports are append-only. An agent rename keeps its author ID and original name
on past reports. Forget leaves history available by report/author ID; registering
the same name again creates a new author, so old reports cannot become its work.
The shared latest view includes current registrations; full history includes
forgotten authors, explicitly labelled. Agent filters refer to current identities.

The six-hour prune job uses `timing.delivery_retention_days` (30 days by default)
for report history too, retaining each author's latest report even if old or
inactive. This prevents inactivity from disappearing into “no report yet.” Retry
keys live with their reports and expire when those historical records are pruned.
The single initial migration and installed-schema repair add the report store
and nullable author identity to existing databases on service startup.

Ordinary agent reports appear in General and the agent's topic, with separate
delivery receipts. Swarm participants, including coordinators, cannot publish human-facing reports.
Only their repository agent reports consolidated progress to the owner. Optional full-report voice messages can be enabled with
`backbone config set telegram.report_audio true` after local speech setup.
See `backbone docs report-audio` for the model, service, voice and FFmpeg setup.
