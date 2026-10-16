# Token usage and optional API-equivalent cost

`backbone usage` shows separate CLI conversations for each agent, with measured
models, tokens and source coverage. `backbone usage --agent NAME --current`
selects the running conversation and its children. A fresh CLI session has its
own history; explicit resume does not duplicate the previous history.

```bash
backbone usage --agent NAME --since 24h
backbone usage session ID
backbone usage --by model --since 1h
backbone usage --cost
backbone usage --json
backbone usage limits
```

Input, cache-read, cache-write and output tokens are disjoint. Reasoning is
already in output. Known children have separate rows; totals include them once.
`--cost` is optional and uses dated standard API rates, not subscription billing.
Unknown prices stay unpriced; unsupported sources stay unavailable, never zero.
`--reprice` uses the current catalog without changing stored price evidence.

`--runtime`, `--since` (inclusive), `--until` (exclusive), `--limit`, `--offset`
and `--no-refresh` filter or page history. Totals cover the full selection.
Quota percentages are last observed account snapshots and must not be summed.

Read `backbone docs token-usage` for supported runtimes, coverage, pricing and
privacy. The quick-start guide remains at `backbone help usage`.
