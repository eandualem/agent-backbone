# Runtime capabilities

Which Backbone capabilities work on which agent CLI. Backbone ships seven
adapters: `claude` (Claude Code), `codex`, `gemini` (Gemini CLI), `opencode`,
`deepcode` (Deep Code), `aider`, and `shell`, a plain shell for testing the
plumbing, not for real use. A capability is meant to behave the same on every
adapter. Where it does not yet, the cell names the open issue.

This table is generated from the capability contract in
`src/agent_backbone/services/runtimes/capabilities.py`, and `backbone doctor`
reports from the same source. A test fails when they disagree, or when a cell
claims support that the adapter's code lacks.

| Mark | Meaning |
|---|---|
| ✅ | Supported: behaves the same as on the other supported runtimes |
| ❌ | Gap: not available on this runtime yet; the issue tracks the fix |
| ? | Unverified: not established on this runtime; treat as unavailable |
| n/a | Cannot exist on this adapter (for example, a plain shell runs no model) |

<!-- capability-table:begin -->
| Capability | `claude` | `codex` | `gemini` | `opencode` | `deepcode` | `aider` | `shell` |
|---|---|---|---|---|---|---|---|
| Message delivery into the session | ✅ | ✅ | ? not verified live | ✅ | ✅ | ? not verified live | ✅ plumbing tests only |
| Folder-trust dialog answered at start | ✅ | ✅ | ✅ `--skip-trust` | n/a | n/a | ? not checked | n/a |
| Brief reaches a fresh session before other work | ✅ | ❌ #290 an earlier launch's brief, and other messages, can arrive first | ? | ✅ | ? | ❌ #290 | n/a |
| Current brief after resume | ❌ #273 the resumed session keeps its stored system prompt | ❌ #273 | ❌ #273 | ❌ #273 | ❌ #273 | ❌ #273 | n/a |
| Brief followed after context compaction | ✅ | ✅ | ? #291 | ❌ #291 | ? #291 | ? #291 | n/a |
| Project AGENTS.md loaded at start | ✅ | ✅ | ❌ #286 reads GEMINI.md unless context.fileName is set | ✅ | ? #286 | ❌ #286 reads only files passed to it | n/a |
| Non-empty user-level instruction file detected | ❌ #274 | ❌ #274 | ❌ #274 | ❌ #274 | ❌ #274 | ❌ #274 | n/a |
| CLI-native memory disabled or detected | ❌ #292 auto-memory is on | ? #292 | ? #292 | ? #292 | ? #292 | ? #292 | n/a |
| State reported by the runtime (hooks) | ✅ | ✅ | ✅ | ✅ | ❌ #275 read from the terminal only | ❌ #275 read from the terminal only | n/a |
| Steer and high-priority events into a working agent | ✅ | ✅ | ❌ #276 | ❌ #276 | ❌ #276 | ❌ #276 | n/a |
| Permission dialog detected and alerted | ✅ | ✅ | ? markers not verified live | ✅ | ❌ #287 dialog not captured | ? markers not verified live | n/a |
| Permission dialog answered (agent approve) | ✅ | ✅ | ❌ #277 | ✅ | ❌ #277 | ❌ #277 | n/a |
| Plan approval answered | ✅ | ❌ #278 | ❌ #278 | ❌ #278 | ❌ #278 | ❌ #278 | n/a |
| Alert when an automatic safety check refuses an action | ✅ | ❌ #279 | ❌ #279 | ❌ #279 | ❌ #279 | ❌ #279 | n/a |
| Browser tab group named after the agent | ✅ with `backbone chrome install` | ❌ #270 | ❌ #270 | ❌ #270 | ❌ #270 | ❌ #270 | n/a |
| Resume the agent's own session | ✅ | ✅ | ✅ | ✅ | ❌ #280 resumes the directory's latest session | ❌ #280 | n/a |
| No-approval mode (unattended) | ✅ | ✅ | ✅ | ✅ | ❌ #281 | ❌ #281 | n/a |
| Writes bounded while unattended | ❌ #285 | ✅ OS sandbox | ❌ #285 | ❌ #285 | ❌ #285 | ❌ #285 | n/a |
| Shared skills linked | ✅ | ✅ | ✅ | ✅ | ❌ #282 | ❌ #282 | n/a |
| Token usage recorded | ✅ | ✅ | ❌ #283 | ✅ | ❌ #283 | ❌ #283 | n/a |
| agent output from the runtime's own record | ✅ | ✅ | ❌ #284 | ❌ #284 | ❌ #284 | ❌ #284 | n/a |
| Deep review run from and for this runtime | ✅ | ✅ | ❌ #288 | ❌ #288 | ❌ #288 | ❌ #288 | n/a |
<!-- capability-table:end -->

## Notes

- **Gemini CLI sign-in.** In a test with Gemini CLI 0.46, one personal Google
  account was refused ("no longer supported for Gemini Code Assist for
  individuals") before any model call; Backbone reports such a session as
  `waiting_for_human`. An API key is the documented alternative. Cells marked
  unverified for Gemini have not been checked against a signed-in session.
- **Deep Code** is `@vegamo/deepcode-cli`, the community CLI DeepSeek's docs
  point to. Its permission dialog has not been captured, so `agent approve`
  refuses it until then.
- `backbone runtimes` shows which of these CLIs are installed locally.
