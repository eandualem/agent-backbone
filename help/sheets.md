# Sheets — create and maintain structured retained workbooks

Use a Sheet for a living tracker or spreadsheet the operator and owning agent
edit together. It contains typed cells, multiple tabs, formulas and saved views;
it is distinct from an HTML Document, a Response and a progress report.

```bash
backbone sheets create --schema
backbone sheets create --file tracker.json --validate
backbone sheets create --file tracker.json --json
backbone sheets --author example-agent --json
backbone sheets show SHEET_ID --revision REVISION_ID --tab TAB_ID --json
backbone sheets edit SHEET_ID --file edit.json --json
backbone sheets history SHEET_ID --json
backbone sheets import --file data.csv --format csv --title Tracker --agent example-agent --json
backbone sheets export SHEET_ID --revision REVISION_ID --format json --output backup.json
backbone sheets feedback SHEET_ID --file feedback.json --json
```

Author requests use your existing registered/signed identity. Operator edits and
feedback set `author: null` and require operator credentials. Preserve the sheet
ID. Edits require the current `base_revision_id` and append immutable revisions;
a stale base returns 409. Keep the draft and explicitly reapply it after reading
the new revision. Retry timeouts with the exact request and same `request_id`.

Read cell ranges at one pinned revision. Use stable row/column IDs when editing
or asking about a sorted view, not visible row ordinals. Formula-like text stays
literal when its input kind is text. Import/export diagnostics identify losses;
CSV/TSV are values-only, while native JSON preserves the supported workbook model.
No Google account integration or full Google Sheets parity is claimed.

`--validate` checks request shape only. Use `--schema` without `--file`. Read
`backbone docs sheets` for operations, typed values, conflict/feedback contracts,
portable formats, calculation boundaries and resource limits.
