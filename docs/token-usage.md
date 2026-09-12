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
contains only its own requests. Overall totals include each row once. Codex's
inherited pre-fork history is excluded. A missing cumulative baseline or counter
reset is marked partial instead of attributing inherited tokens to new work.

Coverage describes retained source evidence, not an independent provider audit:

- `measured`: the source was read through its last complete observation.
- `catching_up`: more data remains; collection continues on the next refresh.
- `partial`: malformed, reset, replaced or otherwise incomplete source evidence.
- `unavailable`: no identified readable source; this does not mean zero usage.

Missing/deleted records cannot be recovered. Tokens may be incomplete for
interrupted requests, unrecorded descendants, or runtime versions that omit
usage. `unpriced` is separate: token counts remain available without a price.

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

Codex JSONL rollouts, Claude Code JSONL conversations and OpenCode's local SQLite
message records are supported. Their schemas were checked against local records;
runtime upgrades can change them. Gemini, Deep Code, Aider and shell currently
report unavailable. This does not claim universal per-request coverage.

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
incomplete final lines retry. Child discovery may lag by up to 30 seconds.
`--no-refresh` reads retained history. `--limit` (1–1000, default 100) and `--offset`
page the displayed items; totals always cover the full selected time range.
`--json` exposes the complete structured response for the selected page.

Only identity, timestamps, token counters, cursor metadata and price evidence go
into Backbone's own database. No prompts or tool output are retained by the usage
collector. Registration files live under Backbone's `state/usage-sessions/`.
They are consumed after their launch identities commit to the database; usage
history remains in the database. Source failures and recovery are visible in
the existing operational diagnostics.
Nothing is required in a user's repository, and existing telemetry/status-line
configuration is preserved. `usage.enabled=false` stops importing; stored history
remains queryable. The authenticated `GET /api/usage` exposes the same views.

The former `backbone usage` quick-start guide is now `backbone help usage`;
`backbone docs usage` remains available. Use `backbone help token-usage` for this
feature's command summary.
