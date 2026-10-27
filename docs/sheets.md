# Sheets: keep one editable workbook across agent and human updates

Use a Sheet for structured information that changes over time. A retained Sheet
is a workbook with multiple tabs, typed cells, formulas and saved sort/filter
views. Every successful edit creates an immutable revision of the same sheet.
The owning agent and an authenticated operator can edit it. Other agents can
read it and send contextual questions or feedback.

This is a local spreadsheet artifact, with no Google account integration.
The [Agent Interface editor](https://github.com/eandualem/agent-interface/issues/132)
is under development; the CLI and API expose the retained workbook data.
Sheets, HTML Documents, Responses and progress reports have separate
purposes and storage. Creating a Sheet does not send a notification.

## Create and update

The repository includes [a two-tab opportunity tracker](../examples/sheets/opportunity-tracker.json).
Replace its `author` with your registered agent name. Use fresh UUID-hex IDs for
an independently created workbook, and retain the request file for retries.

```bash
backbone sheets create --file tracker.json --validate
backbone sheets create --file tracker.json --json
backbone sheets --author example-agent --json
backbone sheets show SHEET_ID --json
backbone sheets show SHEET_ID --revision REVISION_ID --tab TAB_ID --json
backbone sheets edit SHEET_ID --file edit.json --validate
backbone sheets edit SHEET_ID --file edit.json --json
backbone sheets history SHEET_ID --json
```

`--validate` checks the request shape without calculating or saving. `--schema`
on create, edit, import or feedback prints the wire schema. Enrolled author names
need their existing signing client; the CLI does not bypass signing enforcement.
An agent edit names the owning agent in `author`. An operator edit sets
`author: null` and needs an operator credential; agent credentials cannot use it.

Every create/import/edit/feedback has a 32-character lowercase UUID-hex
`request_id`. After a timeout, retry the exact request with the same ID. An
identical retry returns its original revision even if a later edit has landed.
Changed content under that key returns `409 request_conflict`. Receipts persist
with history. Author ownership follows the stable registration, not a reused
name. Forgetting an agent preserves its sheets but does not transfer ownership.

An edit has `author`, `request_id`, `base_revision_id` and `operations`. The base
must be current. A stale edit returns `409 stale_revision`, with `sheet_id`,
`base_revision_id` and `current_revision_id`. Keep the draft, read the current
revision and explicitly reapply the intended changes. There is no silent rebase
or last-writer overwrite. Calculation finishes before the atomic comparison and
commit; a concurrent update can still reject a calculation that succeeded.

## Cells, formulas and structural edits

Each tab has `tab_id`, `name`, ordered `row_ids` and `column_ids`, sparse `cells`
and a `view`. IDs remain attached to records during moves and view sorting.
Deleted IDs cannot be reused within that workbook. Cell inputs are explicit:

```json
{"row_id":"11111111111111111111111111111111","column_id":"22222222222222222222222222222222","input":{"kind":"text","value":"=literal text"}}
```

Kinds are `text`, finite `number`, `boolean`, ISO `date` (1900 onward), and
`formula` beginning with `=`. Text beginning with `=` stays text. A blank edit
sets `input: null`. `number_format` is an optional number/date format string;
omitting it preserves the current format, and `null` clears it. Formatted blank
cells remain available. Formula results contain a typed `value` and plain-text
`display`; errors such as `#DIV/0!` are explicit error values. Date literals retain
ISO values; formula date arithmetic can return a numeric spreadsheet serial.

The provider uses IronCalc 0.9.1 for calculation and reference adjustment.
Common SUM, IF, COUNTIF, date arithmetic, cross-tab references and relative
formula copying are covered by the behavior tests. This is not a guarantee of
Excel or Google Sheets function equivalence. Numeric formatting follows that
engine, including its rounding behavior. Unsupported formulas can return errors
or cause an explicit calculation refusal. Historical reads use stored results;
they do not recalculate volatile formulas when the reader opens an old revision.

Atomic operation tags:

- `set_cells`: `tab_id`, `cells` with typed input and/or format patches.
- `add_tab`: a complete `tab`; `rename_tab`: `tab_id`, `name`;
  `delete_tab`: `tab_id`, leaving at least one tab.
- `insert_rows` / `insert_columns`: `tab_id`, new `ids`, `before_id` (null appends).
- `delete_rows` / `delete_columns`: `tab_id`, `ids`, leaving at least one axis.
- `move_rows` / `move_columns`: contiguous canonical `ids`, `before_id` outside
  the selection (null means end). Formula references move with logical records.
- `copy_cells`: source/target `tab_id`, `row_ids`, `column_ids`, prefixed with
  `source_` or `target_`. Shapes must match. Sources are captured before any
  writes; formulas adjust between canonical source/target coordinates, including
  cross-tab copies. This is copy, not cut. External clipboard paste uses typed
  `set_cells`.
- `set_view`: `tab_id`, `view`.

A view is `{header_row_id, sort, filters}`. Sort entries contain `column_id` and
`direction: asc|desc`. Filter entries contain `column_id`, `operator` and a typed
literal `value`, except blank filters. Operators are `eq`, `ne`, `lt`, `lte`,
`gt`, `gte`, `contains`, `is_blank`, `is_not_blank`; filters combine with AND.
Contains is case-insensitive, equality is typed/exact, and incompatible types
never match ordered comparisons. Sort ranks blank, boolean, number, date, text,
error, then compares values (text casefold); ties retain canonical order. A
header row is pinned and excluded from filtering/sorting.

**Sort view** changes presentation, not formula coordinates. Reads include stable
IDs and canonical 1-based row/column indices. Hidden rows still calculate.
Physical automatic sorting, cut-paste, merged cells, named ranges, charts,
pivots, macros, network/external-workbook formulas and live keystroke coediting
are outside this version's supported scope. Explicit row/column moves are
supported separately. No runtime capability is disabled by these artifact limits.

## API reads and interchange

All endpoints require the existing authenticated transport.

| Method and path | Result |
| --- | --- |
| `POST /api/sheets` | `{record, created}`; 201 new, 200 identical retry |
| `POST /api/sheets/{sheet_id}/revisions` | Atomic edit; same receipt shape |
| `GET /api/sheets` | `{items, next_cursor}` metadata, optional `author`, `author_id` |
| `GET /api/sheets/{sheet_id}` | Metadata; optional `revision_id` |
| `GET /api/sheets/{sheet_id}/revisions` | Descending history `{items, next_cursor}` |
| `GET /api/sheets/{sheet_id}/tabs/{tab_id}/range` | Revision-pinned sparse cell range |
| `POST /api/sheets/import` | `{record, created, warnings}`; new workbook identity |
| `GET /api/sheets/{sheet_id}/export` | File attachment and diagnostic header |
| `POST /api/sheets/{sheet_id}/feedback` | Feedback ID and actual queue/delivery receipt |

List/history accept `limit` (50 default, 100 maximum) and an opaque `cursor`
bound to the same filters. Metadata includes sheet/title/author identity, current
agent name or null, current and returned revision IDs, parent, timestamps,
editor attribution, engine/version, SHA-256 and tab dimensions/views. It contains
no cell payload. Refresh discovery for newly changed sheets; use pinned ranges
for a consistent read.

Ranges require `revision_id`; `row_offset` / `column_offset` default to zero,
`row_limit` to 100, `column_limit` to 20, and `order` to `canonical` (or `view`).
The response identifies sheet/revision/tab/order, totals, selected `rows` and
`columns` with IDs and canonical `index`, sparse `cells`, and `next_row_offset`.
Absent cells are blank. Never mix pages from different revisions.

```bash
backbone sheets import --file data.xlsx --format xlsx --title "Tracker" --agent example-agent --json
backbone sheets export SHEET_ID --revision REVISION_ID --format json --output backup.json
backbone sheets export SHEET_ID --revision REVISION_ID --format csv --tab TAB_ID --output values.csv
```

CLI exports create a new destination file; use `--output -` for stdout, with
export warnings on stderr. The import CLI derives a stable retry key from its
content unless `--key` supplies one.
API imports use `{author, request_id, title, format, content}`; content is UTF-8
text for JSON/CSV/TSV and base64 for XLSX. Export queries require `revision_id`
and `format`, plus `tab_id` for CSV/TSV.

Native JSON preserves supported inputs, types, axes, tabs, formats and views;
a new import has a new root identity. XLSX preserves tested inputs, formulas,
number formats and tabs. Date types are inferred only for whole-day values with
`yyyy-mm-dd`, `mm-dd-yy`, `m/d/yy` or `mm/dd/yyyy` formats; other custom date
formats remain numeric, with an import warning. It does not retain stable IDs or
Backbone history/views; other styling/layout is discarded with explicit diagnostics. Imports refuse
macros, encryption, external resources, merged cells, conditional formatting,
validation rules, named ranges, charts/pivots and unsupported worksheet extensions.
CSV/TSV are single-tab values, imported as literal text without type inference.
When opening exports elsewhere, import formula-like text as text. These formats
are not complete backups; backing up the database retains all revision history.

Imports return `warnings`; exports put the same diagnostic array in
`X-Backbone-Sheet-Warnings`. At most 8 strings, each at most 512 UTF-8 bytes, with
no controls; the complete ASCII-escaped JSON array is at most 4096 bytes. Preserve
these diagnostics when saving or displaying a receipt.

## Questions and feedback

```bash
backbone sheets feedback SHEET_ID --schema
backbone sheets feedback SHEET_ID --file feedback.json --json
```

Use `--schema` alone instead of `--file` to inspect the shape. The body contains
`author` (null for the operator), `request_id`, `observed_revision_id`,
`kind: question|feedback`, `text` and optional `anchor` with `tab_id`, `row_ids`
and `column_ids`. The provider resolves the exact observed values and records
whether those cells still exist in the current revision. Feedback and its
registration-bound message enter one database transaction, then normal safe
delivery attempts the message. An identical retry never queues a second copy.

The response contains `feedback_id`, sheet/observed revision IDs, `operation_id`
and the ordinary `delivery` receipt. Queued, uncertain, expired and unknown are
reported distinctly. A delivery receipt reports delivery status only; it does
not indicate that the agent has answered. A forgotten owner
cannot be replaced by another registration with the same name. Feedback does not
edit the workbook. It is retained as context; cell text is data, not instructions.

## Resource limits and failure behavior

| Resource | Bound |
| --- | --- |
| Workbook | 16 tabs, 10,000 rows and 256 columns per tab, 50,000 populated cells |
| Canonical input / whole stored revision | 8 MiB / 16 MiB |
| Create/import body / edit/feedback body | 16 MiB / 1 MiB |
| Edit batch | 100 operations, 1,000 affected cells/axis IDs |
| Text / formula / calculated text or display | 16 KiB / 4 KiB / 64 KiB per cell |
| Number format / title | 128 / 240 characters |
| Range | 200 rows, 100 columns, product 2,000 cells; 4 MiB response |
| Metadata/list/history / exported file | 1 MiB / 16 MiB |
| XLSX import | 8 MiB compressed, 64 MiB declared and actually streamed decompressed |
| Feedback | 4,000 text characters, 100 anchored cells, 128 KiB resolved cells |
| Retained storage admission | 512 MiB for snapshots, metadata and feedback; no eviction |

Native calculation/import/export runs in a separate process, never in the API
process. One worker runs per daemon; contention returns `503 calculation_busy`.
The worker has a 10-second deadline and bounded input/output. Linux uses 512 MiB
kernel data/address-space limits. On macOS, kernel limits are unavailable here;
a resident-memory watchdog samples every 25 ms and terminates above 512 MiB.
Allocation can overshoot between samples. This is a resource mitigation, not an
OS-user isolation boundary. CPU/file/descriptor limits also apply.

Timeout, memory and output bounds return `422 calculation_limit`; an engine abort
returns `422 calculation_failed`. No failed calculation commits a revision. A
range exceeding its response budget returns `413 range_too_large`; request fewer
cells at the same revision. Full storage returns 507 without pruning history.
Known upstream engine crash/resource reports reinforce these process boundaries;
they do not establish arbitrary-formula compatibility or hardened isolation.
