# CLI presentation convention

The CLI is a product interface for people and a tool interface for agents. Human
output should be clear at a glance; structured output should be complete and
predictable. Presentation must not change runtime behavior, add task tracking or
require files or configuration in a user's repository.

## Human output

- Use a titled table for a collection of comparable records. Use consistent
  labels, stable ordering where order has no operational meaning, and textual
  states such as `valid`, `not set` or `offline`. Color adds emphasis only.
- Use labeled sections for one record or a prose report. Put identity before
  observations, keep evidence distinct from authored claims, and show where to
  retrieve deeper detail. A configured model is not an observed running model.
- Use `cli/presentation.py` for new collection and detail views. It treats values
  as literal text, removes terminal controls, wraps long values and switches to
  labeled records when columns would be too narrow. Do not manually clip values.
- Use rounded borders in an interactive terminal and ASCII borders when output
  is redirected, `TERM=dumb`, or `NO_COLOR` is set. Respect the detected terminal
  width (`COLUMNS` may override it). Narrow output retains every field; it can be
  taller, but does not silently discard data.
- Keep empty states and errors explicit and actionable. A missing description is
  `not described`; missing data must not be mistaken for a healthy or completed
  state. Preserve command exit codes and established stdout/stderr contracts.
- Keep one-step action receipts short. Do not turn a successful `set` or a path
  into a decorative report. Collection/details conventions apply to information
  views, not prompts, raw documents or single values.

The status view remains deliberately compact, with aligned single-line rows.
Its visible ellipses and `--details`/`agent inspect NAME` paths provide full
available context. Other collection views wrap instead of clipping. `status
--details` and `agent inspect` include an agent's stored purpose and tags.
Descriptions and tags are optional; no extra agent declarations are required.

## Machine-readable and raw output

- An existing `--json` path bypasses all presentation helpers: no headings,
  tables, color, prose or extra API calls for presentation. Preserve field names,
  types and meanings. `agent inspect --json` additionally exposes the already
  stored `description` and `tags`; existing fields retain their meanings.
- `skills show`, `templates show`, instruction-preview content, documentation,
  help topics, path commands, shell completion and schema/example output retain
  their raw content contracts. Names-only secret listings never print values.
- Full human detail is available through `skills show NAME`, `skills preview
  AGENT`, `agent inspect NAME`, `updates show ID`, `diagnostics show ID` and the
  existing template preview commands. Hook-provided replies and terminal captures
  may already be bounded at their source; formatting cannot recover unseen text.

## Command audit for issue 220

- **Skills:** list has purpose, tags, receiving agents and validity; preview has
  directories and per-link state; validation has per-agent counts and link state.
  Full descriptions replace the old 100-character clips. `show`/`path` stay raw.
- **Agents:** list exposes purpose and tags alongside saved CLI/model; inspection
  provides repository and directory details.
  It is a configuration inventory; `status` is the live-state view. Inspection
  groups identity and observations, with complete available reply/evidence and
  recent deliveries. Offline inspection labels its limited source explicitly.
- **Status / swarm status:** existing responsive table and watch behavior retained;
  detailed status additionally includes purpose. No readiness inference changed.
- **Swarms:** list separates swarm identity from the members table, retaining roles,
  models, state reasons and details. Creation/disband receipts remain short.
- **Configuration:** list separates setting, value and explicit/default source.
  `get` retains its scalar-first output and help; setters retain their receipts.
- **Runtimes:** availability/state reporting overview, then full example-model,
  effort and unattended details. No hard-coded runtime knowledge in the renderer.
- **Secrets:** names and `set`/`not set` states only; values remain hidden.
- **Templates / instructions:** template and assignment tables, source tables for
  previews; effective instruction text and editing workflow stay unchanged.
  Validation errors and short edit receipts retain their established channels.
- **Reports:** readable labeled sections wrap to terminal width without the old
  character clips. Feed pagination and full-report links remain explicit. Prose
  reports are records rather than forcing their paragraphs into many columns.
- **Diagnostics:** grouped problems, delivery outcomes and operation-record tables;
  labeled evidence and width-aware explanations. Coverage warnings stay visible.
- **Help / docs:** topic/page indexes use tables; requested documents and argparse
  command help retain their content and formatting contracts.
- **Doctor:** sectioned checklist with textual `OK`/`FAIL`, wrapped evidence/hints.
  Setup, service and upgrade steps keep sequential action/status receipts. Tell
  and inbox retain their existing delivery/acknowledgement payload contracts;
  reply, hooks and watch/unwatch retain short confirmations. Shell completion,
  version, raw path and editor output are not collection views.

## Verification for future changes

Issue 222 adds token usage with the same helpers: separate CLI-session rows,
request/model details and opt-in cost. Empty sources remain unavailable, coverage
and pagination stay visible, and JSON bypasses formatting. `backbone help usage`
preserves access to the quick-start guide; `backbone usage --help` describes the
new command.

Exercise representative populated, empty and invalid records at 40, 80 and 120+
columns, including long names, Unicode, markup-like text and terminal controls.
Verify values survive wrapping, errors remain distinguishable, and data cannot
inject styling or terminal commands. Check redirected output and `NO_COLOR`.
For every affected JSON/raw path, verify it still yields the expected payload
without decorations. Prefer shared renderer and command-level contract tests over
brittle snapshots of every border or space.
