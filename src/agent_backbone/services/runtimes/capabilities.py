"""The runtime capability contract: every user-reachable capability, one cell per shipped adapter.

A capability counts as working only when it gives the same user-visible
result in the required set, Claude Code, Codex and OpenCode (``AGENTS.md``,
"Runtime parity"). Every other shipped adapter is on demand and has a
recorded cell too, so what works where is never guessed. This table is the
single source: ``doctor`` reports from it and ``docs/runtime-capabilities.md``
is generated from it (a test fails when either drifts).

A cell is one of:

- ``supported`` — the capability behaves as in the required set, with its
  ``evidence``: a behaviour test or doc file, or a ``live:`` receipt;
- ``gap`` — it does not, yet: on a required runtime an open defect with its
  issue; on an on-demand adapter an issue only once someone needs it there;
- ``unverified`` — not yet established on this runtime; unavailable until it
  is; an issue tracks it on a required runtime;
- ``exception`` — the owner decided, for this named capability, that it is
  not required here: the ``decision`` and its issue are recorded, and it is
  still reported as unavailable;
- ``n/a`` — the capability cannot exist on this adapter, for the reason
  given (a plain shell runs no model). A missing integration or an upstream
  limit is a gap, never ``n/a``.

Every row names its ``implementation``, a ``fallback`` (what the user and
the agent do where it is unavailable) and ``implemented``: the adapter check
the registry test holds every ``supported`` cell to. Where no adapter
attribute shows a capability, the adapter declares it in its own module
(``Runtime.declared_capabilities``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from agent_backbone.services.runtimes.base import Runtime

Status = Literal["supported", "gap", "unverified", "exception", "n/a"]

REQUIRED: tuple[str, ...] = ("claude", "codex", "opencode")
"""The required set: a capability is working only when it works in all three."""

REQUIRED_BASELINE: frozenset[tuple[str, str, int]] = frozenset(
    {
        ("plan-approval", "codex", 278),
        ("refusal-alert", "codex", 279),
        ("browser-group-name", "codex", 270),
        ("bounded-unattended", "claude", 285),
        ("auto-review", "claude", 293),
        ("deep-review", "codex", 288),
        ("brief-after-resume", "opencode", 273),
        ("brief-after-compaction", "opencode", 291),
        ("plan-approval", "opencode", 278),
        ("refusal-alert", "opencode", 279),
        ("browser-group-name", "opencode", 270),
        ("reasoning-effort", "opencode", 296),
        ("bounded-unattended", "opencode", 285),
        ("auto-review", "opencode", 293),
        ("message-authority", "opencode", 294),
        ("message-validate", "opencode", 302),
        ("deep-review", "opencode", 288),
    }
)
"""``(row, runtime, issue)``: required-set cells that shipped before the
requirement (before the contract; OpenCode's before it joined the set, #339)
and are not supported yet. It only shrinks: a fixed cell leaves it, and a new
or changed capability never joins it (the registry test fails on any other
unsupported required-set cell)."""

UNAVAILABLE: frozenset[str] = frozenset({"gap", "unverified", "exception"})


@dataclass(frozen=True)
class Cell:
    status: Status
    issue: int | None = None
    note: str = ""
    evidence: str = ""
    decision: str = ""


@dataclass(frozen=True)
class Capability:
    id: str
    title: str
    implementation: str
    fallback: str
    implemented: Callable[[Runtime], bool]
    cells: Mapping[str, Cell]


def _ok(evidence: str, note: str = "") -> Cell:
    return Cell("supported", note=note, evidence=evidence)


def _gap(issue: int | None = None, note: str = "") -> Cell:
    return Cell("gap", issue=issue, note=note)


def _unverified(issue: int | None = None, note: str = "") -> Cell:
    return Cell("unverified", issue=issue, note=note)


def _na(reason: str) -> Cell:
    return Cell("n/a", note=reason)


def _declared(capability: str) -> Callable[[Runtime], bool]:
    return lambda rt: capability in rt.declared_capabilities


def _row(
    id: str,
    title: str,
    implementation: str,
    *,
    fallback: str,
    implemented: Callable[[Runtime], bool],
    claude: Cell,
    codex: Cell,
    gemini: Cell,
    opencode: Cell,
    deepcode: Cell,
    aider: Cell,
    shell: Cell,
) -> Capability:
    cells = {
        "claude": claude,
        "codex": codex,
        "gemini": gemini,
        "opencode": opencode,
        "deepcode": deepcode,
        "aider": aider,
        "shell": shell,
    }
    return Capability(id, title, implementation, fallback, implemented, cells)


_B1 = "live: #271 batch 1"
_B2 = "live: #271 batch 2 (session record)"
_B3 = "live: #271 batch 3 (scratch probe)"
_B4 = "live: #271 batch 4 (scratch probe)"
_PROVENANCE = "live: #273 post-fix round 3 (forged briefs declined)"
_FRESH = "live: #290 post-fix probe (stale brief retired, current brief first)"
_RESUME = "live: #273 post-fix probe (resume and compaction)"
_GEMINI_STUB = "live: #302 (Gemini CLI 0.46 against a local API stub)"
_LAUNCH = "tests/unit/services/runtimes/test_launch_commands.py"
_NO_MODEL = "a plain shell runs no model"
_USER_FILES = "tests/unit/services/runtimes/test_user_instructions.py"
_MEMORY = "tests/unit/services/runtimes/test_native_memory.py"
_SIGNED = "tests/unit/api/routes/test_api_signing.py"
_API_BOUNDARY = "checked at the API, the same for every runtime"
_CONFIRMED = "tests/unit/api/routes/test_api_confirmations.py"
_VALIDATE = "tests/unit/services/terminal/test_callers.py"
_UNMEASURED = "commands it runs are not yet measured under its pane"
_INBOX_ONLY = "tests/unit/services/test_inbox_only_agents.py"
_NO_CLI = "runs no CLI; the recorded runtime makes no difference"
_HINT = "tests/unit/api/routes/test_api_messages.py"
_QUEUE_ONLY = "read from the queue, the same for every runtime"
_INTERRUPTED = "tests/unit/services/agents/test_interrupted_turn.py"

CAPABILITIES: tuple[Capability, ...] = (
    _row(
        "delivery",
        "Message delivery into the session",
        "src/agent_backbone/services/routing/_delivery.py",
        fallback="Check `agent inspect` for the delivery evidence before relying on it.",
        implemented=lambda rt: True,
        claude=_ok(_B3),
        codex=_ok(_B3),
        gemini=_ok(_GEMINI_STUB),
        opencode=_ok(_B3),
        deepcode=_ok("live: checked during development (Deep Code 0.3.1)"),
        aider=_unverified(note="not verified live"),
        shell=_ok(
            "tests/unit/services/routing/test_delivery_diagnostics.py", "plumbing tests only"
        ),
    ),
    _row(
        "trust",
        "Folder-trust dialog answered at start",
        "src/agent_backbone/services/agents/launch.py",
        fallback="Answer the dialog in the session (`agent attach`).",
        implemented=_declared("trust"),
        claude=_ok(_LAUNCH),
        codex=_ok(_LAUNCH),
        gemini=_ok(_LAUNCH, "`--skip-trust`"),
        opencode=_na("shows no trust dialog"),
        deepcode=_na("shows no trust dialog"),
        aider=_unverified(note="not checked"),
        shell=_na("a plain shell has no trust dialog"),
    ),
    _row(
        "brief-at-start",
        "Brief reaches a fresh session before other work",
        "src/agent_backbone/services/agents/launch.py",
        fallback="Check that the agent's first reply follows its brief; start it fresh if not.",
        implemented=lambda rt: rt.brief_mode != "none",
        claude=_ok(_B3),
        codex=_ok(_FRESH),
        gemini=_ok(_GEMINI_STUB),
        opencode=_ok(_B3),
        deepcode=_unverified(),
        aider=_unverified(note="not verified live"),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "brief-after-resume",
        "Current brief after resume",
        "src/agent_backbone/services/agents/launch.py",
        fallback="Start the agent fresh (`agent start --fresh`) to apply a changed brief.",
        implemented=lambda rt: rt.brief_refresh == "hook_context",
        claude=_ok(_RESUME),
        codex=_ok(_RESUME),
        gemini=_gap(),
        opencode=_gap(273),
        deepcode=_gap(),
        aider=_gap(),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "brief-after-compaction",
        "Brief followed after context compaction",
        "src/agent_backbone/services/agents/launch.py",
        fallback="If a long-running agent stops following its brief, start it fresh.",
        implemented=_declared("brief-after-compaction"),
        claude=_ok(_B4),
        codex=_ok(_B4),
        gemini=_unverified(),
        opencode=_gap(291, "the rule is kept but no longer followed"),
        deepcode=_unverified(),
        aider=_unverified(),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "project-instructions",
        "Project AGENTS.md loaded at start",
        "each CLI loads the project's instruction file itself",
        fallback="Put the instructions in the file the CLI reads, or pass it explicitly.",
        implemented=_declared("project-instructions"),
        claude=_ok(_B1),
        codex=_ok(_B2),
        gemini=_gap(note="reads GEMINI.md unless context.fileName is set"),
        opencode=_ok(_B2),
        deepcode=_ok("live: #286 local stub endpoint (./AGENTS.md sent as a system message)"),
        aider=_gap(note="reads only files passed to it"),
        shell=_na("a plain shell loads no instructions"),
    ),
    _row(
        "global-instructions-detected",
        "Non-empty user-level instruction file detected",
        "src/agent_backbone/cli/setup.py",
        fallback="Check the CLI's user-level instruction file by hand.",
        implemented=lambda rt: type(rt).user_instructions is not Runtime.user_instructions,
        claude=_ok(_USER_FILES),
        codex=_ok(_USER_FILES),
        gemini=_ok(_USER_FILES),
        opencode=_ok(_USER_FILES, "files listed under `instructions` in its config not checked"),
        deepcode=_ok(_USER_FILES),
        aider=_unverified(note="no default user-level file per its docs; not yet verified"),
        shell=_na("a plain shell reads no instruction file"),
    ),
    _row(
        "cli-memory",
        "CLI-native memory disabled or detected",
        "src/agent_backbone/services/runtimes",
        fallback="Turn the CLI's own memory off in its settings.",
        implemented=lambda rt: type(rt).native_memory is not Runtime.native_memory,
        claude=_ok(_MEMORY, "managed settings not read"),
        codex=_ok(_MEMORY, "managed config and the `[memories]` switches not read"),
        gemini=_ok(_MEMORY),
        opencode=_na("keeps no memory of its own (1.18: no memory tool or file)"),
        deepcode=_na("keeps no memory of its own (0.3.1)"),
        aider=_unverified(note="not yet verified"),
        shell=_na("a plain shell keeps no memory"),
    ),
    _row(
        "hook-state",
        "State reported by the runtime (hooks)",
        "src/agent_backbone/hooks",
        fallback="State is read from the terminal, which is slower and less certain.",
        implemented=lambda rt: bool(rt.hook_script),
        claude=_ok("tests/unit/hooks/test_claude_hook.py"),
        codex=_ok(
            "tests/unit/hooks/test_codex_hook.py",
            note="from its first prompt (the brief): Codex runs no hook before its first turn",
        ),
        gemini=_ok("tests/unit/hooks/test_gemini_hook.py"),
        opencode=_ok("tests/unit/hooks/test_opencode_hook.py"),
        deepcode=_gap(note="read from the terminal only"),
        aider=_gap(note="read from the terminal only"),
        shell=_na("a plain shell has no agent turn to report"),
    ),
    _row(
        "interrupted-turn",
        "Interrupted turn reads idle (Escape, refused dialog)",
        "src/agent_backbone/services/agents/_inference.py",
        fallback="The state is corrected when the hook state goes stale (5 minutes).",
        implemented=lambda rt: (
            bool(rt.interrupt_patterns) or "interrupted-turn" in rt.declared_capabilities
        ),
        claude=_ok(_INTERRUPTED, "from the terminal: Claude Code runs no hook on an interrupt"),
        codex=_ok(_INTERRUPTED),
        gemini=_gap(note="a declined dialog runs no hook"),
        opencode=_ok(_INTERRUPTED),
        deepcode=_unverified(note="read from the terminal only"),
        aider=_unverified(note="not verified live"),
        shell=_na("a plain shell has no agent turn to interrupt"),
    ),
    _row(
        "steer",
        "Steer and high-priority events into a working agent",
        "src/agent_backbone/services/routing/_steer.py",
        fallback="Send an ordinary message; it waits until the agent is at its prompt.",
        implemented=lambda rt: rt.hook_context,
        claude=_ok("tests/unit/hooks/test_context.py"),
        codex=_ok("tests/unit/hooks/test_context.py"),
        gemini=_gap(),
        opencode=_ok(
            "tests/unit/hooks/test_context.py",
            note="as a user message in the running turn, after the next tool call",
        ),
        deepcode=_gap(),
        aider=_gap(),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "permission-detected",
        "Permission dialog detected and alerted",
        "src/agent_backbone/services/runtimes/base.py",
        fallback="Watch the session (`agent attach`) for dialogs.",
        implemented=lambda rt: bool(rt.prompt_markers),
        claude=_ok("tests/unit/services/runtimes/test_live_panes.py"),
        codex=_ok("tests/unit/services/runtimes/test_live_panes.py"),
        gemini=_ok("tests/unit/services/runtimes/test_live_panes.py"),
        opencode=_ok("tests/unit/services/runtimes/test_live_panes.py"),
        deepcode=_ok("tests/unit/services/runtimes/test_live_panes.py"),
        aider=_unverified(note="markers not verified live"),
        shell=_na("a plain shell shows no permission dialog"),
    ),
    _row(
        "approve",
        "Permission dialog answered (agent approve)",
        "src/agent_backbone/services/agents/launch.py",
        fallback="Answer the dialog in the session (`agent attach`).",
        implemented=lambda rt: bool(rt.approve_keys),
        claude=_ok("docs/cli.md"),
        codex=_ok("docs/cli.md"),
        gemini=_ok("tests/unit/services/agents/test_launch.py"),
        opencode=_ok("docs/cli.md"),
        deepcode=_ok("tests/unit/services/agents/test_launch.py"),
        aider=_gap(),
        shell=_na("a plain shell shows no permission dialog"),
    ),
    _row(
        "deny",
        "Permission dialog refused (agent deny)",
        "src/agent_backbone/services/agents/launch.py",
        fallback="Refuse the dialog in the session (`agent attach`).",
        implemented=lambda rt: bool(rt.deny_keys),
        claude=_ok("docs/cli.md"),
        codex=_ok("docs/cli.md"),
        gemini=_ok("tests/unit/services/agents/test_launch.py"),
        opencode=_ok("tests/unit/services/agents/test_launch.py"),
        deepcode=_ok("tests/unit/services/agents/test_launch.py"),
        aider=_gap(),
        shell=_na("a plain shell shows no permission dialog"),
    ),
    _row(
        "plan-approval",
        "Plan approval answered",
        "src/agent_backbone/services/agents/launch.py",
        fallback="Answer the plan in the session (`agent attach`).",
        implemented=lambda rt: bool(rt.plan_approve_keys),
        claude=_ok("tests/unit/api/routes/test_api_plans.py"),
        codex=_gap(278),
        gemini=_gap(),
        opencode=_gap(278),
        deepcode=_gap(),
        aider=_gap(),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "refusal-alert",
        "Alert when an automatic safety check refuses an action",
        "src/agent_backbone/services/jobs/escalation.py",
        fallback="Watch the agent's output (`agent output`) for refused actions.",
        implemented=lambda rt: any(event == "PermissionDenied" for event, _ in rt.hook_events),
        claude=_ok("tests/unit/hooks/test_claude_hook.py"),
        codex=_gap(279),
        gemini=_gap(),
        opencode=_gap(279),
        deepcode=_gap(),
        aider=_gap(),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "browser-group-name",
        "Browser tab group named after the agent",
        "src/agent_backbone/hooks/claude_hook.py",
        fallback="A Codex agent can name its own group with its Chrome client's `nameSession`.",
        implemented=_declared("browser-group-name"),
        claude=_ok("tests/unit/hooks/test_chrome_tab_names.py", "with `backbone chrome install`"),
        codex=_gap(270),
        gemini=_gap(),
        opencode=_gap(270),
        deepcode=_gap(),
        aider=_gap(),
        shell=_na("a plain shell drives no browser"),
    ),
    _row(
        "exact-resume",
        "Resume the agent's own session",
        "src/agent_backbone/services/agents/launch.py",
        fallback="Start the agent fresh; its handoff carries the context.",
        implemented=lambda rt: rt.supports_exact_resume,
        claude=_ok(_LAUNCH),
        codex=_ok(_LAUNCH),
        gemini=_ok(_LAUNCH),
        opencode=_ok(_LAUNCH),
        deepcode=_gap(note="resumes the directory's latest session"),
        aider=_gap(),
        shell=_na("a plain shell has no conversation to resume"),
    ),
    _row(
        "reasoning-effort",
        "Reasoning effort chosen with the model (`model:effort`)",
        "src/agent_backbone/services/runtimes/base.py",
        fallback=(
            "Set the effort in the CLI's own settings, or run the agent on Claude Code or Codex."
        ),
        implemented=lambda rt: bool(rt.efforts),
        claude=_ok(_LAUNCH),
        codex=_ok(_LAUNCH),
        gemini=_unverified(note="whether the CLI has an effort setting is not checked"),
        opencode=_gap(296, "the CLI has one; Backbone refuses the effort"),
        deepcode=_ok(_LAUNCH, note="low, high or max, through DEEPCODE_REASONING_EFFORT"),
        aider=_unverified(note="whether the CLI has an effort setting is not checked"),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "unattended",
        "No-approval mode (unattended)",
        "src/agent_backbone/services/runtimes/base.py",
        fallback="Run the agent attended and answer its prompts.",
        implemented=lambda rt: rt.unattended_args is not None,
        claude=_ok(_LAUNCH),
        codex=_ok(_LAUNCH),
        gemini=_ok(_LAUNCH),
        opencode=_ok(_LAUNCH),
        deepcode=_gap(),
        aider=_gap(),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "bounded-unattended",
        "Writes bounded while unattended",
        "src/agent_backbone/services/runtimes/base.py",
        fallback="Run unattended agents only on a sandboxed runtime (Codex).",
        implemented=lambda rt: rt.sandboxed,
        claude=_gap(285),
        codex=_ok(_LAUNCH, "OS sandbox"),
        gemini=_gap(),
        opencode=_gap(285),
        deepcode=_gap(),
        aider=_gap(),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "auto-review",
        "Automatic permission review (agents.auto_review)",
        "src/agent_backbone/services/runtimes/base.py",
        fallback="Review permission prompts yourself, or use the runtime's own policy.",
        implemented=lambda rt: bool(rt.auto_review_args),
        claude=_unverified(293, "auto mode; equivalence not verified"),
        codex=_ok(_LAUNCH, "`--approve-for-me`"),
        gemini=_gap(),
        opencode=_gap(293),
        deepcode=_gap(),
        aider=_gap(),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "skills",
        "Shared skills linked",
        "src/agent_backbone/skills.py",
        fallback="Point the agent at the skill files by path.",
        implemented=lambda rt: bool(rt.skill_dirs),
        claude=_ok("tests/unit/test_skills.py"),
        codex=_ok("tests/unit/api/routes/test_api_skills.py"),
        gemini=_ok("tests/unit/test_skills.py", "live-checked: it lists the linked skills (0.46)"),
        opencode=_ok("tests/unit/test_skills.py"),
        deepcode=_ok("tests/unit/api/routes/test_api_skills.py"),
        aider=_gap(),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "usage",
        "Token usage recorded",
        "src/agent_backbone/usage.py",
        fallback="Read usage in the CLI or its provider's console.",
        implemented=lambda rt: rt.usage_supported,
        claude=_ok("tests/unit/test_usage_accounting.py"),
        codex=_ok("tests/unit/test_usage_accounting.py"),
        gemini=_ok("tests/unit/test_usage_accounting.py", "live-checked with a subagent (0.46)"),
        opencode=_ok("tests/unit/test_usage_accounting.py"),
        deepcode=_gap(),
        aider=_gap(),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "transcript-output",
        "agent output from the runtime's own record",
        "src/agent_backbone/services/agents/transcript.py",
        fallback="`agent output` shows the visible screen instead.",
        implemented=lambda rt: rt.transcript_supported,
        claude=_ok("tests/unit/services/runtimes/test_transcripts.py"),
        codex=_ok("tests/unit/services/runtimes/test_transcripts.py"),
        gemini=_ok("tests/unit/services/runtimes/test_transcripts.py"),
        opencode=_ok("tests/unit/services/runtimes/test_transcripts.py"),
        deepcode=_gap(),
        aider=_gap(),
        shell=_na("a plain shell has no model messages; output is its screen"),
    ),
    _row(
        "provider-failure",
        "Provider capacity or rate-limit failure detected (blocked)",
        "src/agent_backbone/services/runtimes/base.py",
        fallback="Watch the session for provider errors; the agent may look busy or idle.",
        implemented=lambda rt: bool(rt.provider_error_patterns or rt.provider_error_prefixes),
        claude=_ok("tests/unit/services/agents/test_provider_failures.py"),
        codex=_ok("tests/unit/services/agents/test_provider_failures.py"),
        gemini=_gap(),
        opencode=_ok("tests/unit/services/agents/test_provider_failures.py"),
        deepcode=_ok("tests/unit/services/agents/test_provider_failures.py"),
        aider=_gap(),
        shell=_na("a plain shell has no model provider"),
    ),
    _row(
        "observed-model",
        "Running model observed (status)",
        "src/agent_backbone/hooks/backbone_state.py",
        fallback="`status` shows the configured model; check the session for the running one.",
        implemented=_declared("observed-model"),
        claude=_ok("tests/unit/hooks/test_claude_hook.py"),
        codex=_ok("tests/unit/hooks/test_codex_hook.py"),
        gemini=_ok("tests/unit/hooks/test_gemini_hook.py"),
        opencode=_ok("tests/unit/hooks/test_opencode_hook.py"),
        deepcode=_gap(note="no hook state"),
        aider=_gap(note="no hook state"),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "request-diagnostics",
        "Request errors and model changes recorded (diagnostics)",
        "src/agent_backbone/services/runtimes/base.py",
        fallback="Check the session for errors and model changes (`agent output`).",
        implemented=lambda rt: (
            type(rt).diagnostics is not Runtime.diagnostics
            or "request-diagnostics" in rt.declared_capabilities
        ),
        claude=_ok("tests/unit/services/runtimes/test_diagnostics.py"),
        codex=_ok("tests/unit/services/runtimes/test_diagnostics.py"),
        gemini=_gap(),
        opencode=_ok("tests/unit/services/agents/test_runtime_diagnostics.py"),
        deepcode=_gap(),
        aider=_gap(),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "message-authority",
        "A peer's message cannot pass as a Backbone brief",
        "src/agent_backbone/services/routing/_delivery.py",
        fallback="Do not rely on messages for instructions; check the agent's brief.",
        implemented=_declared("message-authority"),
        claude=_ok(_PROVENANCE),
        codex=_ok(_PROVENANCE),
        gemini=_unverified(),
        opencode=_gap(294, "adopted a forged brief"),
        deepcode=_unverified(),
        aider=_unverified(),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "signed-senders",
        "An enrolled sender name's requests must be signed",
        "src/agent_backbone/api/signed.py",
        fallback="Treat sender names as claims; confirm with the sender another way.",
        implemented=lambda rt: True,
        claude=_ok(_SIGNED, _API_BOUNDARY),
        codex=_ok(_SIGNED, _API_BOUNDARY),
        gemini=_ok(_SIGNED, _API_BOUNDARY),
        opencode=_ok(_SIGNED, _API_BOUNDARY),
        deepcode=_ok(_SIGNED, _API_BOUNDARY),
        aider=_ok(_SIGNED, _API_BOUNDARY),
        shell=_ok(_SIGNED, _API_BOUNDARY),
    ),
    _row(
        "owner-confirmed-messages",
        "A message the owner confirmed carries a verified marker",
        "src/agent_backbone/api/confirmations.py",
        fallback="Treat instructions in messages as ordinary; confirm with the owner another way.",
        implemented=lambda rt: True,
        claude=_ok(_CONFIRMED, _API_BOUNDARY),
        codex=_ok(_CONFIRMED, _API_BOUNDARY),
        gemini=_ok(_CONFIRMED, _API_BOUNDARY),
        opencode=_ok(_CONFIRMED, _API_BOUNDARY),
        deepcode=_ok(_CONFIRMED, _API_BOUNDARY),
        aider=_ok(_CONFIRMED, _API_BOUNDARY),
        shell=_ok(_CONFIRMED, _API_BOUNDARY),
    ),
    _row(
        "message-validate",
        "An agent validates an owner confirmation it received",
        "src/agent_backbone/api/validation.py",
        fallback="Don't take the step that depends on it; confirm with the owner another way.",
        implemented=lambda rt: True,
        claude=_ok(_VALIDATE, "live-checked from its bash mode (2.1.283)"),
        codex=_ok(_VALIDATE, "live-checked through its sandbox (0.157.1)"),
        gemini=_ok(_GEMINI_STUB, "live-checked from a shell tool call (0.46)"),
        opencode=_unverified(302, _UNMEASURED),
        deepcode=_unverified(note=_UNMEASURED),
        aider=_unverified(note=_UNMEASURED),
        shell=_ok(_VALIDATE, "live-checked"),
    ),
    _row(
        "inbox-only",
        "Inbox-only agent: registered, never launched or typed into",
        "src/agent_backbone/services/agents/operations.py",
        fallback=(
            "Keep the agent stopped; read its messages with `backbone inbox --agent NAME`"
            " before they expire."
        ),
        implemented=lambda rt: True,
        claude=_ok(_INBOX_ONLY, _NO_CLI),
        codex=_ok(_INBOX_ONLY, _NO_CLI),
        gemini=_ok(_INBOX_ONLY, _NO_CLI),
        opencode=_ok(_INBOX_ONLY, _NO_CLI),
        deepcode=_ok(_INBOX_ONLY, _NO_CLI),
        aider=_ok(_INBOX_ONLY, _NO_CLI),
        shell=_ok(_INBOX_ONLY, _NO_CLI),
    ),
    _row(
        "inbox-hint",
        "New inbox messages hinted on Socket.IO (`inbox:pending`)",
        "src/agent_backbone/api/session_updates.py",
        fallback="Read the inbox (`POST /api/messages/inbox`) on a timer.",
        implemented=lambda rt: True,
        claude=_ok(_HINT, _QUEUE_ONLY),
        codex=_ok(_HINT, _QUEUE_ONLY),
        gemini=_ok(_HINT, _QUEUE_ONLY),
        opencode=_ok(_HINT, _QUEUE_ONLY),
        deepcode=_ok(_HINT, _QUEUE_ONLY),
        aider=_ok(_HINT, _QUEUE_ONLY),
        shell=_ok(_HINT, _QUEUE_ONLY),
    ),
    _row(
        "inbox-escalations",
        "Escalations reach an inbox-only target's inbox",
        "src/agent_backbone/services/jobs/escalation.py",
        fallback="Set `escalation.target` to an agent with a terminal session.",
        implemented=lambda rt: True,
        claude=_ok(_INBOX_ONLY, _NO_CLI),
        codex=_ok(_INBOX_ONLY, _NO_CLI),
        gemini=_ok(_INBOX_ONLY, _NO_CLI),
        opencode=_ok(_INBOX_ONLY, _NO_CLI),
        deepcode=_ok(_INBOX_ONLY, _NO_CLI),
        aider=_ok(_INBOX_ONLY, _NO_CLI),
        shell=_ok(_INBOX_ONLY, _NO_CLI),
    ),
    _row(
        "deep-review",
        "Deep review run from and for this runtime",
        "docs/deep-reviews.md",
        fallback="Run the review with Claude Code or Codex as the reviewer.",
        implemented=_declared("deep-review"),
        claude=_ok("docs/deep-reviews.md"),
        codex=_unverified(288, "a Claude reviewer launched from Codex's sandbox is not measured"),
        gemini=_gap(),
        opencode=_gap(288),
        deepcode=_gap(),
        aider=_gap(),
        shell=_na(_NO_MODEL),
    ),
)


def unavailable(runtime_id: str) -> list[Capability]:
    """Capabilities this runtime does not provide: gaps, unverified cells and exceptions."""
    return [cap for cap in CAPABILITIES if cap.cells[runtime_id].status in UNAVAILABLE]


def _cell_text(cell: Cell) -> str:
    mark = {
        "supported": "✅",
        "gap": "❌",
        "unverified": "?",
        "exception": "⊘",
        "n/a": "n/a",
    }[cell.status]
    parts = [mark]
    if cell.issue is not None:
        parts.append(f"#{cell.issue}")
    if cell.note and cell.status != "n/a":
        parts.append(cell.note)
    return " ".join(parts)


def markdown_table(runtime_ids: tuple[str, ...]) -> str:
    """The contract as the tables in ``docs/runtime-capabilities.md``."""
    lines = [
        "| Capability | " + " | ".join(f"`{rid}`" for rid in runtime_ids) + " |",
        "|---|" + "---|" * len(runtime_ids),
    ]
    for cap in CAPABILITIES:
        cells = " | ".join(_cell_text(cap.cells[rid]) for rid in runtime_ids)
        lines.append(f"| {cap.title} | {cells} |")
    lines += ["", "| Capability | Fallback where it is unavailable |", "|---|---|"]
    lines += [f"| {cap.title} | {cap.fallback} |" for cap in CAPABILITIES]
    return "\n".join(lines)
