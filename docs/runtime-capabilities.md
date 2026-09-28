# Runtime capabilities

Which Backbone capabilities work on which agent CLI. Backbone ships seven
adapters: `claude` (Claude Code), `codex`, `gemini` (Gemini CLI), `opencode`,
`deepcode` (Deep Code), `aider`, and `shell`, a plain shell for testing the
plumbing, not for real use.

Claude Code, Codex and OpenCode are the required set: a capability counts as
working only when it gives the same result in all three, and a gap on one of
them names the open issue that tracks it. Gemini CLI, Deep Code and Aider are
supported on demand: their cells record what works there today, and a gap
names no issue until someone needs that capability on that CLI and opens one
issue for it. Wherever a capability is missing, the second table says what to
do instead.

This page is generated from the capability contract in
`src/agent_backbone/services/runtimes/capabilities.py`, and `backbone doctor`
reports from the same source. A test fails when they disagree, or when a cell
claims support that the adapter's code does not declare.

| Mark | Meaning |
|---|---|
| ✅ | Supported: behaves as in the required set, with a test or live check |
| ❌ | Gap: not available on this runtime yet; the issue, where named, tracks the fix |
| ? | Unverified: not established on this runtime; treat as unavailable |
| ⊘ | Owner exception: not required here by an explicit decision; unavailable |
| n/a | Cannot exist on this adapter (for example, a plain shell runs no model) |

<!-- capability-table:begin -->
| Capability | `claude` | `codex` | `gemini` | `opencode` | `deepcode` | `aider` | `shell` |
|---|---|---|---|---|---|---|---|
| Message delivery into the session | ✅ | ✅ | ✅ | ✅ | ✅ | ? not verified live | ✅ plumbing tests only |
| Folder-trust dialog answered at start | ✅ | ✅ | ✅ `--skip-trust` | n/a | n/a | ? not checked | n/a |
| Brief reaches a fresh session before other work | ✅ | ✅ | ✅ | ✅ | ? | ? not verified live | n/a |
| Current brief after resume | ✅ | ✅ | ❌ | ❌ #273 | ❌ | ❌ | n/a |
| Brief followed after context compaction | ✅ | ✅ | ? | ❌ #291 the rule is kept but no longer followed | ? | ? | n/a |
| Project AGENTS.md loaded at start | ✅ | ✅ | ❌ reads GEMINI.md unless context.fileName is set | ✅ | ✅ | ❌ reads only files passed to it | n/a |
| Non-empty user-level instruction file detected | ✅ | ✅ | ✅ | ✅ files listed under `instructions` in its config not checked | ✅ | ? no default user-level file per its docs; not yet verified | n/a |
| CLI-native memory disabled or detected | ✅ managed settings not read | ✅ managed config and the `[memories]` switches not read | ✅ | n/a | n/a | ? not yet verified | n/a |
| State reported by the runtime (hooks) | ✅ | ✅ from its first prompt (the brief): Codex runs no hook before its first turn | ✅ | ✅ | ❌ read from the terminal only | ❌ read from the terminal only | n/a |
| Steer and high-priority events into a working agent | ✅ | ✅ | ❌ | ✅ as a user message in the running turn, after the next tool call | ❌ | ❌ | n/a |
| Permission dialog detected and alerted | ✅ | ✅ | ✅ | ✅ | ✅ | ? markers not verified live | n/a |
| Permission dialog answered (agent approve) | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | n/a |
| Permission dialog refused (agent deny) | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | n/a |
| Plan approval answered | ✅ | ❌ #278 | ❌ | ❌ #278 | ❌ | ❌ | n/a |
| Alert when an automatic safety check refuses an action | ✅ | ❌ #279 | ❌ | ❌ #279 | ❌ | ❌ | n/a |
| Browser tab group named after the agent | ✅ with `backbone chrome install` | ❌ #270 | ❌ | ❌ #270 | ❌ | ❌ | n/a |
| Resume the agent's own session | ✅ | ✅ | ✅ | ✅ | ❌ resumes the directory's latest session | ❌ | n/a |
| Reasoning effort chosen with the model (`model:effort`) | ✅ | ✅ | ? whether the CLI has an effort setting is not checked | ❌ #296 the CLI has one; Backbone refuses the effort | ✅ low, high or max, through DEEPCODE_REASONING_EFFORT | ? whether the CLI has an effort setting is not checked | n/a |
| No-approval mode (unattended) | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | n/a |
| Writes bounded while unattended | ❌ #285 | ✅ OS sandbox | ❌ | ❌ #285 | ❌ | ❌ | n/a |
| Automatic permission review (agents.auto_review) | ? #293 auto mode; equivalence not verified | ✅ `--approve-for-me` | ❌ | ❌ #293 | ❌ | ❌ | n/a |
| Shared skills linked | ✅ | ✅ | ✅ live-checked: it lists the linked skills (0.46) | ✅ | ✅ | ❌ | n/a |
| Token usage recorded | ✅ | ✅ | ✅ live-checked with a subagent (0.46) | ✅ | ❌ | ❌ | n/a |
| agent output from the runtime's own record | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | n/a |
| Provider capacity or rate-limit failure detected (blocked) | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | n/a |
| Running model observed (status) | ✅ | ✅ | ✅ | ✅ | ❌ no hook state | ❌ no hook state | n/a |
| Request errors and model changes recorded (diagnostics) | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | n/a |
| A peer's message cannot pass as a Backbone brief | ✅ | ✅ | ? | ❌ #294 adopted a forged brief | ? | ? | n/a |
| An enrolled sender name's requests must be signed | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime |
| A message the owner confirmed carries a verified marker | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime |
| An agent validates an owner confirmation it received | ✅ live-checked from its bash mode (2.1.283) | ✅ live-checked through its sandbox (0.157.1) | ✅ live-checked from a shell tool call (0.46) | ? #302 commands it runs are not yet measured under its pane | ? commands it runs are not yet measured under its pane | ? commands it runs are not yet measured under its pane | ✅ live-checked |
| Inbox-only agent: registered, never launched or typed into | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference |
| New inbox messages hinted on Socket.IO (`inbox:pending`) | ✅ read from the queue, the same for every runtime | ✅ read from the queue, the same for every runtime | ✅ read from the queue, the same for every runtime | ✅ read from the queue, the same for every runtime | ✅ read from the queue, the same for every runtime | ✅ read from the queue, the same for every runtime | ✅ read from the queue, the same for every runtime |
| Escalations reach an inbox-only target's inbox | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference |
| Deep review run from and for this runtime | ✅ | ? #288 a Claude reviewer launched from Codex's sandbox is not measured | ❌ | ❌ #288 | ❌ | ❌ | n/a |

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
| Permission dialog refused (agent deny) | Refuse the dialog in the session (`agent attach`). |
| Plan approval answered | Answer the plan in the session (`agent attach`). |
| Alert when an automatic safety check refuses an action | Watch the agent's output (`agent output`) for refused actions. |
| Browser tab group named after the agent | A Codex agent can name its own group with its Chrome client's `nameSession`. |
| Resume the agent's own session | Start the agent fresh; its handoff carries the context. |
| Reasoning effort chosen with the model (`model:effort`) | Set the effort in the CLI's own settings, or run the agent on Claude Code or Codex. |
| No-approval mode (unattended) | Run the agent attended and answer its prompts. |
| Writes bounded while unattended | Run unattended agents only on a sandboxed runtime (Codex). |
| Automatic permission review (agents.auto_review) | Review permission prompts yourself, or use the runtime's own policy. |
| Shared skills linked | Point the agent at the skill files by path. |
| Token usage recorded | Read usage in the CLI or its provider's console. |
| agent output from the runtime's own record | `agent output` shows the visible screen instead. |
| Provider capacity or rate-limit failure detected (blocked) | Watch the session for provider errors; the agent may look busy or idle. |
| Running model observed (status) | `status` shows the configured model; check the session for the running one. |
| Request errors and model changes recorded (diagnostics) | Check the session for errors and model changes (`agent output`). |
| A peer's message cannot pass as a Backbone brief | Do not rely on messages for instructions; check the agent's brief. |
| An enrolled sender name's requests must be signed | Treat sender names as claims; confirm with the sender another way. |
| A message the owner confirmed carries a verified marker | Treat instructions in messages as ordinary; confirm with the owner another way. |
| An agent validates an owner confirmation it received | Don't take the step that depends on it; confirm with the owner another way. |
| Inbox-only agent: registered, never launched or typed into | Keep the agent stopped; read its messages with `backbone inbox --agent NAME` before they expire. |
| New inbox messages hinted on Socket.IO (`inbox:pending`) | Read the inbox (`POST /api/messages/inbox`) on a timer. |
| Escalations reach an inbox-only target's inbox | Set `escalation.target` to an agent with a terminal session. |
| Deep review run from and for this runtime | Run the review with Claude Code or Codex as the reviewer. |
<!-- capability-table:end -->

## Notes

- **Gemini CLI sign-in.** In a test with Gemini CLI 0.46, one personal Google
  account was refused ("no longer supported for Gemini Code Assist for
  individuals") before any model call; Backbone reports such a session as
  `waiting_for_human`. An API key is the documented alternative. Cells marked
  unverified for Gemini have not been checked against a signed-in session.
- **Folder trust.** OpenCode and Deep Code show no folder-trust dialog, so
  that row is n/a for them. Every other n/a is the plain shell's.
- **Deep Code** is `@vegamo/deepcode-cli`, the community CLI DeepSeek's docs
  point to. Its permission dialog is detected, so the session reads as
  `waiting_for_human` while it shows, and `agent approve` and `agent deny`
  answer it.
- `backbone runtimes` shows which of these CLIs are installed locally.
