---
name: backbone-sheets
description: Create, read, edit and export retained Backbone spreadsheet workbooks using stable cells and revision-aware updates.
metadata:
  backbone-tags: all
---

Read `backbone help sheets` and `backbone docs sheets`; use `--schema` for the
specific create, edit or import request. This is Backbone's retained workbook,
not Google Sheets or live control of a desktop spreadsheet application.

Read the current workbook and pin the relevant revision before editing. Preserve
sheet, tab, row and column identities. Submit edits with the observed base revision;
on a stale-base response, keep the intended changes and reconcile them with a fresh
read rather than overwriting. Retry an uncertain request with exactly the same
request ID and content. Formula-like text remains text unless explicitly authored
as a formula. Read calculation/import warnings and verify returned cells.

Export the pinned revision in the requested format and disclose supported losses.
For contextual discussion, use the shared conversation with a revision-pinned cell
selection. Respect selection limits; never silently replace an oversized selection
with a label that loses its meaning. A saved workbook is not proof of native app
acceptance or a completed owner-message round trip.
