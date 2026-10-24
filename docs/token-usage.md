# Token usage by agent and CLI session

`backbone usage` shows the tokens used by each identified CLI conversation.
If an agent works in Codex, then starts Claude Code, those are separate rows.
Resuming a conversation keeps its existing row and records another launch;
starting a fresh conversation creates another row. Models come from request
observations, so changing the configured model never relabels old usage.

```bash
backbone usage
backbone usage --agent researcher --current
backbone usage --agent researcher --runtime codex --since 24h
backbone usage session ID
backbone usage --by model --since 2026-09-12T00:00:00Z
backbone usage --since 2026-09-12T10:00:00Z --until 2026-09-12T11:00:00Z --json
backbone usage --cost
backbone usage limits
```

The overview gives the agent, CLI, session ID, observed models, tokens and
coverage. Use its ID in `usage session ID` for request observations and the
input/cache/output breakdown. A runtime's full conversation ID also works when
unambiguous. `--current` includes children of the currently observed conversation.
It measures usage reported so far; an in-flight request may not have reported
its final counters yet. It is not a tokens-per-second meter.

Time filters select request timestamps in UTC: `--since` is inclusive and
`--until` exclusive. Relative values such as `1h` are supported by the CLI;
the API takes timezone-qualified timestamps. Use a time range across agents
for a demonstration without creating a task or issue association.

## What the numbers mean

Four non-overlapping categories make up **total tokens**: uncached input,
cache reads, cache writes and output. Reasoning is included in output; its
separate detail is a subset, never added twice. A cache hit still consumes
tokens even though its API price is lower. Repeatedly reading the same context
on separate requests counts once per request, as the runtime reports it.

Codex cumulative counters become deltas. Claude's streamed message revisions
replace the same request instead of adding another request. OpenCode's separate
reasoning counter joins text output. Repeated collection and explicit resume do
not duplicate observations. Request IDs and available turn IDs are in JSON.
The most recent authoritative request revision wins, including corrections
that lower a count. Original request timestamps remain stable for filtering.

Runtime-proven children have separate rows linked by `parent_id`; the parent row
contains only its own requests. A child identified before its parent is linked
when runtime evidence becomes available. Overall totals include each row once. Codex's
inherited pre-fork history is excluded. OpenCode also excludes inherited fork context
using its native session boundary. A missing cumulative baseline or counter
reset is marked partial instead of attributing inherited tokens to new work.

Coverage describes retained source evidence, not an independent provider audit:

- `measured`: the source was read through its last complete observation.
- `catching_up`: more data remains; collection continues on the next refresh.
- `partial`: malformed, reset, replaced or otherwise incomplete source evidence.
- `unavailable`: no identified readable source; this does not mean zero usage.

OpenCode's current session total can include migrated summaries, title generation
or reverted work that is no longer attributable from its message records. Backbone
compares those sources after catching up and marks mismatches as `partial`, with an
explanation. It retains measured message values and previously collected ledger
entries; it does not add overlapping aggregate totals or invent model/date details.
This describes incomplete current source evidence, even when the retained ledger
already contains some of the removed history.

Missing/deleted records cannot be recovered. When a conversation's source file
is gone, Backbone searches for it once, then records it as `source file is gone`
and stops searching for that parent; the usage already collected stays. Readable
child transcripts are still discovered and collected independently. Codex uses
its shared lineage scan for child discovery even when a parent is gone. The file is read again
if it returns to the same path, and searched for again when the conversation is
launched again (resumed). Tokens may be incomplete for
interrupted requests, unrecorded descendants, or runtime versions that omit
usage. `unpriced` is separate: token counts remain available without a price.

## Context-size note

An agent that does not work issue by issue has no natural point to start
fresh, and its conversation keeps growing. Each monitor tick reads the context
size of the most recent request in every live agent's current conversation.
When it first reaches 200,000 tokens, the agent is told on its next turn:

```text
[via:backbone context] Your conversation is at 205k tokens. When you finish your current task, write your handoff and start a fresh session (backbone agent restart).
```

It is told again at each further 100,000 (300k, 400k, …). Each level is noted
once per conversation, also across backbone restarts, and a fresh conversation
starts over; a note not yet taken is dropped then, and also when a compaction
brings the conversation below its level (a compaction's own hook event can still
hand over a note offered just before it). Nothing is noted while the
conversation's record is still being read or could not be read. The note arrives through the
runtime's hook, like a steer: it never starts a turn and never interrupts the
agent. Restarting stays the
agent's decision; the backbone never restarts it. Swarm members are not noted,
since they end with their swarm. Claude Code, Codex and OpenCode receive the
note; other runtimes don't yet (see [Runtime capabilities](runtime-capabilities.md)).
Change the levels with `usage.context_note_tokens` and
`usage.context_note_step_tokens` in [Configuration](configuration.md#usage).

## Optional cost

`--cost` adds the **standard API-equivalent estimate**, not a subscription bill.
It answers what the recorded tokens would cost at the stored model's standard
published token rates. The calculation prices uncached input, cache reads,
cache writes and output independently. It applies known long-context thresholds
and distinguishes five-minute and one-hour cache writes. Unknown cache duration
with different rates, missing context size, partial observations, or an unknown
exact model price remain unpriced. Mixed totals show the priced subtotal as partial.

The bundled catalog was checked on 2026-09-12 against
[OpenAI pricing](https://developers.openai.com/api/docs/pricing) and
[Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing).
It covers explicitly listed model IDs; aliases are never guessed. Each priced
observation retains its rates, source and date. `usage session ID --cost` exposes
that basis. `--reprice --cost` recalculates a view using the current configured
catalog without rewriting historical estimates.

This is deliberately a **standard service-tier scenario**. The observed tier
is retained separately; priority/fast/batch discounts or surcharges, provider
residency charges, subscription fees, tools, images, search and taxes are excluded.
The estimate is not a reconstruction of the customer's actual invoice. A
runtime-reported cost, when present (currently OpenCode), is retained separately
as `reported_cost_usd` in request JSON and never added to the calculated estimate.

## Sources and limits

Codex JSONL rollouts, Claude Code JSONL conversations, Gemini CLI JSONL sessions
and OpenCode's local SQLite message records are supported. Their schemas were
checked against local records; runtime upgrades can change them. Deep Code, Aider
and shell currently report unavailable. This does not claim universal per-request
coverage: Gemini CLI records the tokens of its conversation's requests only, not
those of the smaller requests it makes for itself, such as choosing a model or
compressing the history.

`usage limits` shows Codex's last recorded allowance windows with observation
time, percentage and reset, marking expired windows. These are snapshots rather
than a fresh account query. Several sessions may share an account; percentages
must not be added and are not inferred from session tokens. Claude allowance is
currently unavailable: Backbone does not replace the user's status line to obtain it.

Existing observed conversations can be adopted without restarting an agent.
Backbone only imports sessions with an agent identity from its hooks/state, plus
runtime-proven descendants. It does not sweep unrelated standalone CLI history
into an agent's totals. Historical conversations that Backbone never identified
are unavailable. Custom runtime home directories are taken from the agent's
saved environment; already discovered source paths are retained.

Collection uses the existing monitor and refreshes on query, without typing into
or interrupting a terminal. JSONL reads are bounded to 8 MiB per source per pass;
incomplete final lines retry. Oversized records are skipped in bounded chunks,
marking coverage partial so later observations can still be collected. Unchanged
JSONL files are checked without reopening them. Child discovery may lag by up to 30 seconds.
`--no-refresh` reads retained history. `--limit` (1–1000, default 100) and `--offset`
page the displayed items; totals always cover the full selected time range.
Each view computes its totals and request page from the same database read,
processing history in bounded batches.
`--json` exposes the complete structured response for the selected page.

Only identity, timestamps, token counters, cursor metadata and price evidence go
into Backbone's own database. No prompts or tool output are retained by the usage
collector. Registration files live under Backbone's `state/usage-sessions/`.
They are consumed after their launch identities commit to the database; usage
history remains in the database. A registration the database refuses, such as one
that conflicts with a session's existing owner, is reported once and moved to
`state/usage-sessions/rejected/`, which collection never reads; the owner keeps
its history. Source failures and recovery are visible in
the existing operational diagnostics.
Nothing is required in a user's repository, and existing telemetry/status-line
configuration is preserved. `usage.enabled=false` stops importing; stored history
remains queryable. The authenticated `GET /api/usage` exposes the same views.

The former `backbone usage` quick-start guide is now `backbone help usage`;
`backbone docs usage` remains available. Use `backbone help token-usage` for this
feature's command summary.
