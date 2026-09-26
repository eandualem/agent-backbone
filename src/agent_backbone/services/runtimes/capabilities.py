"""The runtime capability contract: every user-reachable capability, one cell per shipped adapter.

A capability counts as working only when it gives the same user-visible
result in the required pair, Claude Code and Codex (``AGENTS.md``, "Runtime
parity"). Every other shipped adapter has a recorded cell too, so what works
where is never guessed. This table is the single source: ``doctor`` reports
from it and ``docs/runtime-capabilities.md`` is generated from it (a test
fails when either drifts).

A cell is one of:

- ``supported`` — the capability behaves as in the required pair, with its
  ``evidence``: a behaviour test or doc file, or a ``live:`` receipt;
- ``gap`` — it does not, yet: an open defect with its issue;
- ``unverified`` — not yet established on this runtime; unavailable until it is;
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

REQUIRED: tuple[str, ...] = ("claude", "codex")
"""The required pair: a capability is working only when it works in both."""

REQUIRED_BASELINE: frozenset[tuple[str, str, int]] = frozenset(
    {
        ("global-instructions-detected", "claude", 274),
        ("global-instructions-detected", "codex", 274),
        ("cli-memory", "claude", 292),
        ("cli-memory", "codex", 292),
        ("plan-approval", "codex", 278),
        ("refusal-alert", "codex", 279),
        ("browser-group-name", "codex", 270),
        ("bounded-unattended", "claude", 285),
        ("auto-review", "claude", 293),
        ("deep-review", "codex", 288),
        ("request-diagnostics", "claude", 304),
    }
)
"""``(row, runtime, issue)``: required-pair cells that shipped before the
contract and are not supported yet. It only shrinks: a fixed cell leaves it,
and a new or changed capability never joins it (the registry test fails on
any other unsupported required-pair cell)."""

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


def _gap(issue: int, note: str = "") -> Cell:
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
_LAUNCH = "tests/unit/services/runtimes/test_launch_commands.py"
_NO_MODEL = "a plain shell runs no model"

CAPABILITIES: tuple[Capability, ...] = (
    _row(
        "delivery",
        "Message delivery into the session",
        "src/agent_backbone/services/routing/_delivery.py",
        fallback="Check `agent inspect` for the delivery evidence before relying on it.",
        implemented=lambda rt: True,
        claude=_ok(_B3),
        codex=_ok(_B3),
        gemini=_unverified(302, note="not verified live"),
        opencode=_ok(_B3),
        deepcode=_ok("live: checked during development (Deep Code 0.3.1)"),
        aider=_unverified(302, note="not verified live"),
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
        aider=_unverified(302, note="not checked"),
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
        gemini=_unverified(302),
        opencode=_ok(_B3),
        deepcode=_unverified(302),
        aider=_unverified(302, note="not verified live"),
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
        gemini=_gap(273),
        opencode=_gap(273),
        deepcode=_gap(273),
        aider=_gap(273),
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
        gemini=_unverified(291),
        opencode=_gap(291, "the rule is kept but no longer followed"),
        deepcode=_unverified(291),
        aider=_unverified(291),
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
        gemini=_gap(286, "reads GEMINI.md unless context.fileName is set"),
        opencode=_ok(_B2),
        deepcode=_unverified(286),
        aider=_gap(286, "reads only files passed to it"),
        shell=_na("a plain shell loads no instructions"),
    ),
    _row(
        "global-instructions-detected",
        "Non-empty user-level instruction file detected",
        "src/agent_backbone/cli/setup.py",
        fallback="Check the CLI's user-level instruction file by hand.",
        implemented=_declared("global-instructions-detected"),
        claude=_gap(274),
        codex=_gap(274),
        gemini=_gap(274),
        opencode=_gap(274),
        deepcode=_gap(274),
        aider=_gap(274),
        shell=_na("a plain shell reads no instruction file"),
    ),
    _row(
        "cli-memory",
        "CLI-native memory disabled or detected",
        "src/agent_backbone/services/runtimes",
        fallback="Turn the CLI's own memory off in its settings.",
        implemented=_declared("cli-memory"),
        claude=_gap(292, "auto-memory is on"),
        codex=_unverified(292),
        gemini=_unverified(292),
        opencode=_unverified(292),
        deepcode=_unverified(292),
        aider=_unverified(292),
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
        deepcode=_gap(275, "read from the terminal only"),
        aider=_gap(275, "read from the terminal only"),
        shell=_na("a plain shell has no agent turn to report"),
    ),
    _row(
        "steer",
        "Steer and high-priority events into a working agent",
        "src/agent_backbone/services/routing/_steer.py",
        fallback="Send an ordinary message; it waits until the agent is at its prompt.",
        implemented=lambda rt: rt.hook_context,
        claude=_ok("tests/unit/hooks/test_context.py"),
        codex=_ok("tests/unit/hooks/test_context.py"),
        gemini=_gap(276),
        opencode=_gap(276),
        deepcode=_gap(276),
        aider=_gap(276),
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
        gemini=_unverified(302, note="markers not verified live"),
        opencode=_ok("tests/unit/services/runtimes/test_live_panes.py"),
        deepcode=_gap(287, "dialog not captured"),
        aider=_unverified(302, note="markers not verified live"),
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
        gemini=_gap(277),
        opencode=_ok("docs/cli.md"),
        deepcode=_gap(277),
        aider=_gap(277),
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
        gemini=_gap(299),
        opencode=_gap(299),
        deepcode=_gap(299),
        aider=_gap(299),
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
        gemini=_gap(278),
        opencode=_gap(278),
        deepcode=_gap(278),
        aider=_gap(278),
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
        gemini=_gap(279),
        opencode=_gap(279),
        deepcode=_gap(279),
        aider=_gap(279),
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
        gemini=_gap(270),
        opencode=_gap(270),
        deepcode=_gap(270),
        aider=_gap(270),
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
        deepcode=_gap(280, "resumes the directory's latest session"),
        aider=_gap(280),
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
        gemini=_unverified(296, "whether the CLI has an effort setting is not checked"),
        opencode=_gap(296, "the CLI has one; Backbone refuses the effort"),
        deepcode=_gap(296, "the CLI has one; Backbone refuses the effort"),
        aider=_unverified(296, "whether the CLI has an effort setting is not checked"),
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
        deepcode=_gap(281),
        aider=_gap(281),
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
        gemini=_gap(285),
        opencode=_gap(285),
        deepcode=_gap(285),
        aider=_gap(285),
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
        gemini=_gap(293),
        opencode=_gap(293),
        deepcode=_gap(293),
        aider=_gap(293),
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
        gemini=_unverified(302, note="linked by the shared code; not tested for this runtime"),
        opencode=_unverified(302, note="linked by the shared code; not tested for this runtime"),
        deepcode=_gap(282),
        aider=_gap(282),
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
        gemini=_gap(283),
        opencode=_ok("tests/unit/test_usage_accounting.py"),
        deepcode=_gap(283),
        aider=_gap(283),
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
        gemini=_gap(284),
        opencode=_gap(284),
        deepcode=_gap(284),
        aider=_gap(284),
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
        gemini=_gap(295),
        opencode=_ok("tests/unit/services/agents/test_provider_failures.py"),
        deepcode=_gap(295),
        aider=_gap(295),
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
        gemini=_unverified(302),
        opencode=_gap(303),
        deepcode=_gap(275, "no hook state"),
        aider=_gap(275, "no hook state"),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "request-diagnostics",
        "Request errors and model changes recorded (diagnostics)",
        "src/agent_backbone/services/runtimes/codex.py",
        fallback="Check the session for errors and model changes (`agent output`).",
        implemented=lambda rt: type(rt).diagnostics is not Runtime.diagnostics,
        claude=_gap(304),
        codex=_ok("tests/unit/services/runtimes/test_diagnostics.py"),
        gemini=_gap(304),
        opencode=_gap(304),
        deepcode=_gap(304),
        aider=_gap(304),
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
        gemini=_unverified(294),
        opencode=_gap(294, "adopted a forged brief"),
        deepcode=_unverified(294),
        aider=_unverified(294),
        shell=_na(_NO_MODEL),
    ),
    _row(
        "deep-review",
        "Deep review run from and for this runtime",
        "docs/deep-reviews.md",
        fallback="Run the review with Claude Code or Codex as the reviewer.",
        implemented=_declared("deep-review"),
        claude=_ok("docs/deep-reviews.md"),
        codex=_unverified(288, "a Claude reviewer launched from Codex's sandbox is not measured"),
        gemini=_gap(288),
        opencode=_gap(288),
        deepcode=_gap(288),
        aider=_gap(288),
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
