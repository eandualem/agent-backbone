# Runtime capabilities

Which Backbone capabilities work on which agent CLI. Backbone ships seven
adapters: `claude` (Claude Code), `codex`, `gemini` (Gemini CLI), `opencode`,
`deepcode` (Deep Code), `aider`, and `shell`, a plain shell for testing the
terminal plumbing. Shell notifications stay in the inbox as data.

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

Some capabilities depend on what a CLI draws on screen, the keys it takes or
what its hooks send. On those rows a ✅ names the CLI version it was last
verified on, for example ✅ (2.1.284). A later release of that CLI may change
the behaviour without any change in Backbone, so check the capability again
after upgrading past that version. "(version not recorded)" means the check
that established it did not note the version. `backbone doctor` lists these
versions for the CLIs installed on your machine and notes where the installed
version differs.

<!-- capability-table:begin -->

| Capability | `claude` | `codex` | `gemini` | `opencode` | `deepcode` | `aider` | `shell` |
|---|---|---|---|---|---|---|---|
| Owned Python launch helpers preserve the engine inventory | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Explicit first-agent setup with durable retry receipts | ✅ working-installation live check pending | ✅ working-installation live check pending | ❌ installer hook preparation is not implemented | ✅ working-installation live check pending | ❌ installer hook preparation is not implemented | ❌ installer hook preparation is not implemented | n/a |
| Read-only runtime discovery for setup | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Current agent-authored checklists with conditional updates and contextual feedback | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime |
| GitHub acknowledgements bound to registration and successful publication | ✅ (2.1.310) Registration-bound payload tests; live check pending normal restart. | ✅ (0.160.0) Registration-bound payload tests; live check pending normal restart. | ✅ (0.46.0) Registration-bound payload tests; live check pending normal restart. | ✅ (2.1.4) Registration-bound plugin tests; live check pending normal restart. | ❌ no publication receipt hook | ❌ no publication receipt hook | n/a |
| Retained spreadsheet workbooks, calculated revisions and contextual feedback | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime |
| Durable HTML Documents with immutable revisions and authenticated reads | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime |
| Per-artifact conversations: owner messages delivered through the queue, agent replies | ✅ checked at the API, the same for every runtime; delivered as a queued message, like backbone tell; current-plan adapter: tests/unit/api/routes/test_api_plan_conversations.py; Sheet adapter: tests/unit/api/routes/test_api_sheet_conversations.py | ✅ checked at the API, the same for every runtime; delivered as a queued message, like backbone tell; current-plan adapter: tests/unit/api/routes/test_api_plan_conversations.py; Sheet adapter: tests/unit/api/routes/test_api_sheet_conversations.py | ✅ checked at the API, the same for every runtime; delivered as a queued message, like backbone tell; current-plan adapter: tests/unit/api/routes/test_api_plan_conversations.py; Sheet adapter: tests/unit/api/routes/test_api_sheet_conversations.py | ✅ checked at the API, the same for every runtime; delivered as a queued message, like backbone tell; current-plan adapter: tests/unit/api/routes/test_api_plan_conversations.py; Sheet adapter: tests/unit/api/routes/test_api_sheet_conversations.py | ✅ checked at the API, the same for every runtime; delivered as a queued message, like backbone tell; current-plan adapter: tests/unit/api/routes/test_api_plan_conversations.py; Sheet adapter: tests/unit/api/routes/test_api_sheet_conversations.py | ? message delivery into Aider is not verified live | n/a |
| Stored questions, answers and actions with durable topic delivery | ✅ checked at the API, the same for every runtime; topic delivery: tests/unit/services/integrations/telegram/test_responses.py | ✅ checked at the API, the same for every runtime; topic delivery: tests/unit/services/integrations/telegram/test_responses.py | ✅ checked at the API, the same for every runtime; topic delivery: tests/unit/services/integrations/telegram/test_responses.py | ✅ checked at the API, the same for every runtime; topic delivery: tests/unit/services/integrations/telegram/test_responses.py | ✅ checked at the API, the same for every runtime; topic delivery: tests/unit/services/integrations/telegram/test_responses.py | ✅ checked at the API, the same for every runtime; topic delivery: tests/unit/services/integrations/telegram/test_responses.py | ✅ checked at the API, the same for every runtime; topic delivery: tests/unit/services/integrations/telegram/test_responses.py |
| Retained observations of reconciled state over the API | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime |
| Configured instruction sources and text over the API | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ source preview only; no instruction injection |
| Message delivery into the session | ✅ (2.1.284) | ✅ (0.157.1) | ✅ (0.46.0) | ✅ (1.18.32) | ✅ (0.3.1) | ? not verified live | n/a |
| Folder-trust dialog answered at start | ✅ (2.1.284) | ✅ (0.157.1) | ✅ (version not recorded) `--skip-trust` | n/a | n/a | ? not checked | n/a |
| Brief reaches a fresh session before other work | ✅ native system-prompt file | ✅ SessionStart developer context; new transport awaits a working-session live check | ✅ | ✅ native instructions file; new transport awaits a working-session live check | ? | ? not verified live | n/a |
| Current brief after resume | ✅ (2.1.284) | ✅ (0.157.1) | ❌ | ❌ #273 native instructions now load on resume; working-session check pending | ❌ | ❌ | n/a |
| Brief followed after context compaction | ✅ (2.1.282) | ✅ (0.156.1) | ? | ❌ #291 the rule is kept but no longer followed | ? | ? | n/a |
| Project AGENTS.md loaded at start | ✅ | ✅ | ❌ reads GEMINI.md unless context.fileName is set | ✅ | ✅ | ❌ reads only files passed to it | n/a |
| Non-empty user-level instruction file detected | ✅ | ✅ | ✅ | ✅ files listed under `instructions` in its config not checked | ✅ | ? no default user-level file per its docs; not yet verified | n/a |
| CLI-native memory disabled or detected | ✅ managed settings not read | ✅ managed config and the `[memories]` switches not read | ✅ | n/a | n/a | ? not yet verified | n/a |
| Installed CLI version compared with the verified one (`doctor`) | ✅ | ✅ | ✅ | ✅ | ✅ | ? not yet verified | n/a |
| Replacement state excludes previous launch hooks and submissions | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| State reported by the runtime (hooks) | ✅ (2.1.284) | ✅ (0.157.1) from its first work prompt: Codex runs no hook before its first turn | ✅ (0.46.0) | ✅ (1.18.32) | ❌ read from the terminal only | ❌ read from the terminal only | n/a |
| Interrupted turn reads idle (Escape, refused dialog) | ✅ (2.1.284) from the terminal: Claude Code runs no hook on an interrupt | ✅ (0.157.1) | ❌ a declined dialog runs no hook | ✅ (1.18.32) | ? read from the terminal only | ? not verified live | n/a |
| Steer and high-priority events into a working agent | ✅ (2.1.284) a steer that names its turn is handed over only in that turn, also after an Escape | ✅ (0.157.1) a command still running when its call returns hands it over when it exits (0.157.1); a steer that names its turn is handed over only in that turn, also after a provider failure | ❌ | ✅ (1.18.32) as a user message in the running turn, after the next tool call | ❌ | ❌ | n/a |
| Permission dialog detected and alerted | ✅ (2.1.296) | ✅ (0.157.1) | ✅ (0.46.0) | ✅ (1.18.32) | ✅ (0.3.1) | ? markers not verified live | n/a |
| Permission dialog answered (agent approve) | ✅ (2.1.284) | ✅ (0.157.1) | ✅ (0.46.0) | ✅ (1.18.32) | ✅ (0.3.1) | ❌ | n/a |
| Permission dialog refused (agent deny) | ✅ (2.1.284) | ✅ (0.157.1) | ✅ (0.46.0) | ✅ (1.18.32) | ✅ (0.3.1) | ❌ | n/a |
| Plan approval answered | ✅ (2.1.284) | ✅ (0.157.1) in Plan mode (`/plan`) | ❌ | ❌ #278 requires switching agents in the native session, not structured plan approval | ❌ | ❌ | n/a |
| Alert when an automatic safety check refuses an action | ✅ (2.1.282) | ✅ (0.160.0) its automatic reviewer (`agents.auto_review`) | ❌ | ✅ (2.1.4) 2.1.4+ typed policy denials; native simulated fixture, working-agent check pending | ❌ | ❌ | n/a |
| Browser tab group named after the agent | ✅ (version not recorded) with `backbone chrome install` | ❌ #270 | ❌ | ❌ #270 | ❌ | ❌ | n/a |
| Resume the agent's own session | ✅ | ✅ | ✅ | ✅ | ❌ resume refused: exact session selection unavailable | ❌ | n/a |
| Fleet snapshot resumes the saved session exactly | ✅ | ✅ | ✅ | ✅ | ❌ reported as exact_resume_unsupported | ❌ reported as exact_resume_unsupported | n/a |
| Reasoning effort chosen with the model (`model:effort`, OpenCode `model::effort`) | ✅ | ✅ | ? whether the CLI has an effort setting is not checked | ✅ requires current @opencode/cli (2.1.4+); model::effort | ✅ low, high or max, through DEEPCODE_REASONING_EFFORT | ? whether the CLI has an effort setting is not checked | n/a |
| No-approval mode (unattended) | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | n/a |
| Writes bounded while unattended | ❌ #285 | ✅ OS sandbox; on Linux, git add and commit fail inside it, so swarms refuse unattended Codex members there (#437) | ❌ | ❌ #285 | ❌ | ❌ | n/a |
| Automatic permission review (agents.auto_review) | ? #293 auto mode; equivalence not verified | ✅ `--approve-for-me` | ❌ | ❌ #293 | ❌ | ❌ | n/a |
| Shared skills linked | ✅ | ✅ | ✅ live-checked: it lists the linked skills (0.46) | ✅ | ✅ | ❌ | n/a |
| Repository skills reach the agent's CLI | ✅ (2.1.294) live-checked: follows a relative link between skill folders | ✅ (0.160.0) live-checked: follows a relative link between skill folders | ❌ which skill folders it reads beyond `.agents/skills` is not measured | ✅ (1.18.32) live-checked: follows a relative link between skill folders | ❌ which skill folders it reads beyond `.agents/skills` is not measured | ❌ | n/a |
| Skill and tag changes reach a running agent | ✅ (2.1.294) live-checked: loads a skill linked mid-session | ✅ (0.160.0) live-checked: loads a skill linked mid-session | ❌ whether it loads a skill mid-session is not measured | ✅ (1.18.35) its plugin reloads skills between turns (live-checked) | ❌ whether it loads a skill mid-session is not measured | ❌ | n/a |
| Every skill an agent's CLI loads, listed by source | ✅ (2.1.294) live-checked against its /skills list | ✅ (0.160.0) live-checked against its skill list | ❌ which skill folders it reads beyond `.agents/skills` is not measured | ✅ (1.18.32) live-checked: `opencode debug skill` | ❌ which skill folders it reads beyond `.agents/skills` is not measured | ❌ | n/a |
| Token usage recorded | ✅ | ✅ | ✅ live-checked with a subagent (0.46) | ✅ | ❌ | ❌ | n/a |
| Context-size note recommending a fresh session | ✅ (2.1.284) the hook-context path is live-checked with steers on this version; the note's own live check follows deployment | ✅ (0.157.1) the hook-context path is live-checked with steers on this version; the note's own live check follows deployment | ❌ usage is recorded, but its hook takes no offered context | ✅ (1.18.32) the hook-context path is live-checked with steers on this version; the note's own live check follows deployment | ❌ | ❌ | n/a |
| Drafted feedback to the CLI's vendor off unless the agent opts in | ✅ (2.1.296) `--disallowedTools SendFeedback` and CLAUDE_CODE_SEND_FEEDBACK=0 at launch; set the variable to 1 in the agent's env to opt in | n/a | n/a | n/a | n/a | n/a | n/a |
| agent output from the runtime's own record | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | n/a |
| Provider capacity or rate-limit failure detected (blocked) | ✅ (2.1.284) reported by its StopFailure hook | ✅ (0.160.0) from the terminal: Codex runs no hook for a failed turn | ❌ | ✅ (1.18.32) | ✅ (0.3.1) | ❌ | n/a |
| Running model observed (status) | ✅ (2.1.267) | ✅ (0.156.1) | ✅ (0.46.0) | ✅ (1.18.32) | ❌ no hook state | ❌ no hook state | n/a |
| Request errors and model changes recorded (diagnostics) | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | n/a |
| A peer's message cannot pass as a Backbone brief | ✅ | ✅ | ? | ❌ #294 adopted a forged brief | ? | ? | n/a |
| Private Unix listener checks the connected OS account | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime |
| Registration-scoped credentials, self-switching and persistent client pickup | ✅ Shared authority/profile tests plus tests/unit/services/jobs/test_transitions.py for replacement during restart; natural self-switch live check pending. | ✅ Shared authority/profile tests plus tests/unit/services/jobs/test_transitions.py for replacement during restart; natural self-switch live check pending. | ✅ Shared authority/profile tests plus tests/unit/services/jobs/test_transitions.py for replacement during restart; natural self-switch live check pending. | ✅ Shared authority/profile tests plus tests/unit/services/jobs/test_transitions.py for replacement during restart; natural self-switch live check pending. | ✅ Shared authority/profile tests plus tests/unit/services/jobs/test_transitions.py for replacement during restart; natural self-switch live check pending. | ✅ Shared authority/profile tests plus tests/unit/services/jobs/test_transitions.py for replacement during restart; natural self-switch live check pending. | ✅ Shared authority/profile tests plus tests/unit/services/jobs/test_transitions.py for replacement during restart; natural self-switch live check pending. |
| Retained notifications cannot reach a replacement registration | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference |
| An enrolled sender name's requests must be signed | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime |
| A message the owner confirmed carries a verified marker | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime | ✅ checked at the API, the same for every runtime |
| An agent validates an owner confirmation it received | ✅ live-checked from its bash mode (2.1.283) | ✅ live-checked through its sandbox (0.157.1) | ✅ live-checked from a shell tool call (0.46) | ✅ live-checked from a bash tool call (1.18.32) | ? commands it runs are not yet measured under its pane | ? commands it runs are not yet measured under its pane | ✅ live-checked |
| Inbox-only agent: registered, never launched or typed into | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference |
| New inbox messages hinted on Socket.IO (`inbox:pending`) | ✅ read from the queue, the same for every runtime | ✅ read from the queue, the same for every runtime | ✅ read from the queue, the same for every runtime | ✅ read from the queue, the same for every runtime | ✅ read from the queue, the same for every runtime | ✅ read from the queue, the same for every runtime | ✅ read from the queue, the same for every runtime |
| Escalations reach an inbox-only target's inbox | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference | ✅ runs no CLI; the recorded runtime makes no difference |
| Deep review run from and for this runtime | ✅ | ✅ completed Opus and Astra reviews launched from Codex | ❌ | ✅ OpenCode 2.1.4 with Kimi K3 low launched Astra high and returned its findings | ❌ | ❌ | n/a |

| Capability | Fallback where it is unavailable |
|---|---|
| Owned Python launch helpers preserve the engine inventory | Replace an older payload with a corrected build; reinstall saved hook and Chrome host commands with that build. Do not edit its inventory. |
| Explicit first-agent setup with durable retry receipts | Use the documented agent-start flow; do not retry an uncertain launch or greeting as a new request. |
| Read-only runtime discovery for setup | Binary presence is not login; use the runtime's visible sign-in flow. |
| Current agent-authored checklists with conditional updates and contextual feedback | Keep the current checklist locally until the provider is available. |
| GitHub acknowledgements bound to registration and successful publication | Unbound receipts cannot acknowledge work; restart older sessions normally. |
| Retained spreadsheet workbooks, calculated revisions and contextual feedback | Keep the workbook locally until a Backbone with Sheets is available. |
| Durable HTML Documents with immutable revisions and authenticated reads | Keep authored HTML locally until a Backbone with Documents is available. |
| Per-artifact conversations: owner messages delivered through the queue, agent replies | Ask about an artifact with backbone tell; the answer is not kept with it. |
| Stored questions, answers and actions with durable topic delivery | Use ordinary replies; they have no separately stored answer record. |
| Retained observations of reconciled state over the API | Read current state and delivery records; earlier states cannot be reconstructed. |
| Configured instruction sources and text over the API | Read the preview with `backbone instructions preview NAME --json`. |
| Message delivery into the session | Check `agent inspect` for the delivery evidence before relying on it. |
| Folder-trust dialog answered at start | Answer the dialog in the session (`agent attach`). |
| Brief reaches a fresh session before other work | Check that the agent's first reply follows its brief; start it fresh if not. |
| Current brief after resume | Start the agent fresh (`agent start --fresh`) to apply a changed brief. |
| Brief followed after context compaction | If a long-running agent stops following its brief, start it fresh. |
| Project AGENTS.md loaded at start | Put the instructions in the file the CLI reads, or pass it explicitly. |
| Non-empty user-level instruction file detected | Check the CLI's user-level instruction file by hand. |
| CLI-native memory disabled or detected | Turn the CLI's own memory off in its settings. |
| Installed CLI version compared with the verified one (`doctor`) | Compare `<cli> --version` with the versions in this table. |
| Replacement state excludes previous launch hooks and submissions | Inspect the replacement terminal; readiness does not establish ongoing liveness. |
| State reported by the runtime (hooks) | State is read from the terminal, which is slower and less certain. |
| Interrupted turn reads idle (Escape, refused dialog) | An interrupted agent may keep reading busy or waiting; check the session (`agent attach`). |
| Steer and high-priority events into a working agent | Send an ordinary message; it waits until the agent is at its prompt. |
| Permission dialog detected and alerted | Watch the session (`agent attach`) for dialogs. |
| Permission dialog answered (agent approve) | Answer the dialog in the session (`agent attach`). |
| Permission dialog refused (agent deny) | Refuse the dialog in the session (`agent attach`). |
| Plan approval answered | Answer the plan in the session (`agent attach`). |
| Alert when an automatic safety check refuses an action | Watch the agent's output (`agent output`) for refused actions. |
| Browser tab group named after the agent | A Codex agent can name its own group with its Chrome client's `nameSession`. |
| Resume the agent's own session | Start the agent fresh; its handoff carries the context. |
| Fleet snapshot resumes the saved session exactly | The agent is reported as not resumed; start it fresh and its handoff carries the context. |
| Reasoning effort chosen with the model (`model:effort`, OpenCode `model::effort`) | Set the effort in the CLI's own settings, or run the agent on Claude Code or Codex. |
| No-approval mode (unattended) | Run the agent attended and answer its prompts. |
| Writes bounded while unattended | Run unattended agents only on a sandboxed runtime (Codex). |
| Automatic permission review (agents.auto_review) | Review permission prompts yourself, or use the runtime's own policy. |
| Shared skills linked | Point the agent at the skill files by path. |
| Repository skills reach the agent's CLI | Keep the skill where that CLI reads, or point the agent at it by path. |
| Skill and tag changes reach a running agent | Restart the agent: its next launch links the change. |
| Every skill an agent's CLI loads, listed by source | Ask the agent to list its skills; `skills preview` still shows the shared ones. |
| Token usage recorded | Read usage in the CLI or its provider's console. |
| Context-size note recommending a fresh session | Check the conversation size with `backbone usage`; restart at a task boundary. |
| Drafted feedback to the CLI's vendor off unless the agent opts in | Turn drafted feedback off in the CLI's own settings. |
| agent output from the runtime's own record | `agent output` shows the visible screen instead. |
| Provider capacity or rate-limit failure detected (blocked) | Watch the session for provider errors; the agent may look busy or idle. |
| Running model observed (status) | `status` shows the configured model; check the session for the running one. |
| Request errors and model changes recorded (diagnostics) | Check the session for errors and model changes (`agent output`). |
| A peer's message cannot pass as a Backbone brief | Do not rely on messages for instructions; check the agent's brief. |
| Private Unix listener checks the connected OS account | Configure the private socket; do not send credentials to unverified TCP. |
| Registration-scoped credentials, self-switching and persistent client pickup | Use an operator credential for administrative changes. |
| Retained notifications cannot reach a replacement registration | Inspect retained history; send a new message to the intended registration. |
| An enrolled sender name's requests must be signed | Treat sender names as claims; confirm with the sender another way. |
| A message the owner confirmed carries a verified marker | Treat instructions in messages as ordinary; confirm with the owner another way. |
| An agent validates an owner confirmation it received | Don't take the step that depends on it; confirm with the owner another way. |
| Inbox-only agent: registered, never launched or typed into | Keep the agent stopped; read its messages with `backbone inbox --agent NAME` before they expire. |
| New inbox messages hinted on Socket.IO (`inbox:pending`) | Read the inbox (`POST /api/messages/inbox`) on a timer. |
| Escalations reach an inbox-only target's inbox | Set `escalation.target` to an agent with a terminal session. |
| Deep review run from and for this runtime | Run the review with Claude Code or Codex as the reviewer. |

<!-- capability-table:end -->

## Notes

- **Remote approval request binding.** Approval and denial checkmarks mean the
  runtime's terminal controls are supported, including Codex. They do not claim
  atomic binding to a native request. See the [known limitation and future
  integration requirements](security.md#known-limitation-remote-approval-request-binding).

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
