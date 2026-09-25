# Runtime capabilities

Which Backbone capabilities work on which agent CLI. Backbone ships seven
adapters: `claude` (Claude Code), `codex`, `gemini` (Gemini CLI), `opencode`,
`deepcode` (Deep Code), `aider`, and `shell`, a plain shell for testing the
plumbing, not for real use.

Claude Code and Codex are the required pair: a capability counts as working
only when it gives the same result in both. Every other adapter has its own
recorded coverage below: where a capability is missing there, the cell names
the open issue and the second table says what to do instead.

This page is generated from the capability contract in
`src/agent_backbone/services/runtimes/capabilities.py`, and `backbone doctor`
reports from the same source. A test fails when they disagree, or when a cell
claims support that the adapter's code does not declare.

| Mark | Meaning |
|---|---|
| ✅ | Supported: behaves as in Claude Code and Codex, with a test or live check |
| ❌ | Gap: not available on this runtime yet; the issue tracks the fix |
| ? | Unverified: not established on this runtime; treat as unavailable |
| ⊘ | Owner exception: not required here by an explicit decision; unavailable |
| n/a | Cannot exist on this adapter (for example, a plain shell runs no model) |

<!-- capability-table:begin -->
| Capability | `claude` | `codex` | `gemini` | `opencode` | `deepcode` | `aider` | `shell` |
|---|---|---|---|---|---|---|---|
| Message delivery into the session | ✅ | ✅ | ? not verified live | ✅ | ✅ | ? not verified live | ✅ plumbing tests only |
| Folder-trust dialog answered at start | ✅ | ✅ | ✅ `--skip-trust` | n/a | n/a | ? not checked | n/a |
| Brief reaches a fresh session before other work | ✅ | ❌ #290 an earlier launch's brief, and other messages, can arrive first | ? | ✅ | ? | ❌ #290 | n/a |
| Current brief after resume | ❌ #273 the resumed session keeps its stored system prompt | ❌ #273 | ❌ #273 | ❌ #273 | ❌ #273 | ❌ #273 | n/a |
| Brief followed after context compaction | ✅ | ✅ | ? #291 | ❌ #291 the rule is kept but no longer followed | ? #291 | ? #291 | n/a |
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
| Automatic permission review (agents.auto_review) | ? #293 auto mode; equivalence not verified | ✅ `--approve-for-me` | ❌ #293 | ❌ #293 | ❌ #293 | ❌ #293 | n/a |
| Shared skills linked | ✅ | ✅ | ? linked by the shared code; not tested for this runtime | ? linked by the shared code; not tested for this runtime | ❌ #282 | ❌ #282 | n/a |
| Token usage recorded | ✅ | ✅ | ❌ #283 | ✅ | ❌ #283 | ❌ #283 | n/a |
| agent output from the runtime's own record | ✅ | ✅ | ❌ #284 | ❌ #284 | ❌ #284 | ❌ #284 | n/a |
| Provider capacity or rate-limit failure detected (blocked) | ✅ | ✅ | ❌ #295 | ✅ | ❌ #295 | ❌ #295 | n/a |
| A peer's message cannot pass as a Backbone brief | ✅ | ✅ | ? #294 | ❌ #294 adopted a forged brief | ? #294 | ? #294 | n/a |
| Deep review run from and for this runtime | ✅ | ✅ | ❌ #288 | ❌ #288 | ❌ #288 | ❌ #288 | n/a |

| Capability | Fallback where it is unavailable |
|---|---|
| Message delivery into the session | Check `agent inspect` for the delivery evidence before relying on it. |
| Folder-trust dialog answered at start | Answer the dialog in the session (`agent attach`). |
| Brief reaches a fresh session before other work | Check that the agent's first reply follows its brief; start it fresh if not. |
| Current brief after resume | Start the agent fresh (`agent start --fresh`) to apply a changed brief. |
| Brief followed after context compaction | If a long-running agent stops following its brief, start it fresh. |
| Project AGENTS.md loaded at start | Put the instructions in the file the CLI reads, or pass it explicitly. |
| Non-empty user-level instruction file detected | Check the CLI's user-level instruction file by hand. |
| CLI-native memory disabled or detected | Turn the CLI's own memory off in its settings. |
| State reported by the runtime (hooks) | State is read from the terminal, which is slower and less certain. |
| Steer and high-priority events into a working agent | Send an ordinary message; it waits until the agent is at its prompt. |
| Permission dialog detected and alerted | Watch the session (`agent attach`) for dialogs. |
| Permission dialog answered (agent approve) | Answer the dialog in the session (`agent attach`). |
| Plan approval answered | Answer the plan in the session (`agent attach`). |
| Alert when an automatic safety check refuses an action | Watch the agent's output (`agent output`) for refused actions. |
| Browser tab group named after the agent | A Codex agent can name its own group with its Chrome client's `nameSession`. |
| Resume the agent's own session | Start the agent fresh; its handoff carries the context. |
| No-approval mode (unattended) | Run the agent attended and answer its prompts. |
| Writes bounded while unattended | Run unattended agents only on a sandboxed runtime (Codex). |
| Automatic permission review (agents.auto_review) | Review permission prompts yourself, or use the runtime's own policy. |
| Shared skills linked | Point the agent at the skill files by path. |
| Token usage recorded | Read usage in the CLI or its provider's console. |
| agent output from the runtime's own record | `agent output` shows the visible screen instead. |
| Provider capacity or rate-limit failure detected (blocked) | Watch the session for provider errors; the agent may look busy or idle. |
| A peer's message cannot pass as a Backbone brief | Do not rely on messages for instructions; check the agent's brief. |
| Deep review run from and for this runtime | Run the review with Claude Code or Codex as the reviewer. |
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
