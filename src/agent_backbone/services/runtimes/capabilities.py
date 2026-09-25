"""The runtime capability contract: every user-reachable capability, one cell per shipped adapter.

A Backbone capability is never designed, merged or released for only one CLI
(``AGENTS.md``, "Runtime parity"). This table is the single source for what
works where: ``doctor`` reports from it and ``docs/runtime-capabilities.md``
is generated from it (a test fails when either drifts).

A cell is one of:

- ``supported`` — the capability behaves the same on this runtime;
- ``gap`` — it does not, yet; an open defect with its issue;
- ``unverified`` — implemented or plausible, but the behaviour is not yet
  established on this runtime; counts as unavailable;
- ``n/a`` — the capability cannot exist on this adapter, for the reason
  given (a plain shell runs no model). A missing integration or an upstream
  limit is a gap, never ``n/a``.

Every row names its ``implementation`` (a source path, or the CLI itself),
and every ``supported`` cell its ``evidence``: a behaviour test or doc file,
or a ``live:`` receipt. ``implemented`` optionally names the adapter
attribute behind a row: the registry test fails when a cell claims
``supported`` for a runtime whose adapter lacks it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from agent_backbone.services.runtimes.base import Runtime

Status = Literal["supported", "gap", "unverified", "n/a"]


@dataclass(frozen=True)
class Cell:
    status: Status
    issue: int | None = None
    note: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class Capability:
    id: str
    title: str
    implementation: str
    cells: Mapping[str, Cell]
    implemented: Callable[[Runtime], bool] | None = None


def _ok(evidence: str, note: str = "") -> Cell:
    return Cell("supported", note=note, evidence=evidence)


def _gap(issue: int, note: str = "") -> Cell:
    return Cell("gap", issue=issue, note=note)


def _unverified(issue: int | None = None, note: str = "") -> Cell:
    return Cell("unverified", issue=issue, note=note)


def _na(reason: str) -> Cell:
    return Cell("n/a", note=reason)


_NO_MODEL = _na("a plain shell runs no model")


def _row(
    id: str,
    title: str,
    implementation: str,
    *,
    claude: Cell,
    codex: Cell,
    gemini: Cell,
    opencode: Cell,
    deepcode: Cell,
    aider: Cell,
    shell: Cell = _NO_MODEL,
    implemented: Callable[[Runtime], bool] | None = None,
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
    return Capability(id, title, implementation, cells, implemented)


CAPABILITIES: tuple[Capability, ...] = (
    _row(
        "delivery",
        "Message delivery into the session",
        "src/agent_backbone/services/routing/_delivery.py",
        claude=_ok("live: #271 batch 3 (scratch probe)"),
        codex=_ok("live: #271 batch 3 (scratch probe)"),
        gemini=_unverified(note="not verified live"),
        opencode=_ok("live: #271 batch 3 (scratch probe)"),
        deepcode=_ok("live: checked during development (Deep Code 0.3.1)"),
        aider=_unverified(note="not verified live"),
        shell=_ok("tests/unit/services/routing/test_session_bridge.py", "plumbing tests only"),
    ),
    _row(
        "trust",
        "Folder-trust dialog answered at start",
        "src/agent_backbone/services/agents/launch.py",
        claude=_ok("tests/unit/services/runtimes/test_launch_commands.py"),
        codex=_ok("tests/unit/services/runtimes/test_launch_commands.py"),
        gemini=_ok("tests/unit/services/runtimes/test_launch_commands.py", "`--skip-trust`"),
        opencode=_na("shows no trust dialog"),
        deepcode=_na("shows no trust dialog"),
        aider=_unverified(note="not checked"),
        shell=_na("a plain shell has no trust dialog"),
    ),
    _row(
        "brief-at-start",
        "Brief reaches a fresh session before other work",
        "src/agent_backbone/services/agents/launch.py",
        claude=_ok("live: #271 batch 3 (scratch probe)"),
        codex=_gap(290, "an earlier launch's brief, and other messages, can arrive first"),
        gemini=_unverified(),
        opencode=_ok("live: #271 batch 3 (scratch probe)"),
        deepcode=_unverified(),
        aider=_gap(290),
        implemented=lambda rt: rt.brief_mode != "none",
    ),
    _row(
        "brief-after-resume",
        "Current brief after resume",
        "src/agent_backbone/services/agents/launch.py",
        claude=_gap(273, "the resumed session keeps its stored system prompt"),
        codex=_gap(273),
        gemini=_gap(273),
        opencode=_gap(273),
        deepcode=_gap(273),
        aider=_gap(273),
    ),
    _row(
        "brief-after-compaction",
        "Brief followed after context compaction",
        "src/agent_backbone/services/agents/launch.py",
        claude=_ok("live: #271 batch 4 (scratch probe)"),
        codex=_ok("live: #271 batch 4 (scratch probe)"),
        gemini=_unverified(291),
        opencode=_gap(291),
        deepcode=_unverified(291),
        aider=_unverified(291),
    ),
    _row(
        "project-instructions",
        "Project AGENTS.md loaded at start",
        "each CLI loads the project's instruction file itself",
        claude=_ok("live: #271 batch 1"),
        codex=_ok("live: #271 batch 2 (session record)"),
        gemini=_gap(286, "reads GEMINI.md unless context.fileName is set"),
        opencode=_ok("live: #271 batch 2 (session record)"),
        deepcode=_unverified(286),
        aider=_gap(286, "reads only files passed to it"),
    ),
    _row(
        "global-instructions-detected",
        "Non-empty user-level instruction file detected",
        "src/agent_backbone/cli/setup.py",
        claude=_gap(274),
        codex=_gap(274),
        gemini=_gap(274),
        opencode=_gap(274),
        deepcode=_gap(274),
        aider=_gap(274),
    ),
    _row(
        "cli-memory",
        "CLI-native memory disabled or detected",
        "src/agent_backbone/services/runtimes",
        claude=_gap(292, "auto-memory is on"),
        codex=_unverified(292),
        gemini=_unverified(292),
        opencode=_unverified(292),
        deepcode=_unverified(292),
        aider=_unverified(292),
    ),
    _row(
        "hook-state",
        "State reported by the runtime (hooks)",
        "src/agent_backbone/hooks",
        claude=_ok("tests/unit/hooks/test_claude_hook.py"),
        codex=_ok("tests/unit/hooks/test_codex_hook.py"),
        gemini=_ok("tests/unit/hooks/test_gemini_hook.py"),
        opencode=_ok("tests/unit/hooks/test_opencode_hook.py"),
        deepcode=_gap(275, "read from the terminal only"),
        aider=_gap(275, "read from the terminal only"),
        shell=_na("a plain shell has no agent turn to report"),
        implemented=lambda rt: bool(rt.hook_script),
    ),
    _row(
        "steer",
        "Steer and high-priority events into a working agent",
        "src/agent_backbone/services/routing/_steer.py",
        claude=_ok("tests/unit/hooks/test_context.py"),
        codex=_ok("tests/unit/hooks/test_context.py"),
        gemini=_gap(276),
        opencode=_gap(276),
        deepcode=_gap(276),
        aider=_gap(276),
        implemented=lambda rt: rt.hook_context,
    ),
    _row(
        "permission-detected",
        "Permission dialog detected and alerted",
        "src/agent_backbone/services/runtimes/base.py",
        claude=_ok("tests/unit/services/runtimes/test_live_panes.py"),
        codex=_ok("tests/unit/services/runtimes/test_live_panes.py"),
        gemini=_unverified(note="markers not verified live"),
        opencode=_ok("tests/unit/services/runtimes/test_live_panes.py"),
        deepcode=_gap(287, "dialog not captured"),
        aider=_unverified(note="markers not verified live"),
    ),
    _row(
        "approve",
        "Permission dialog answered (agent approve)",
        "src/agent_backbone/services/agents/launch.py",
        claude=_ok("docs/cli.md"),
        codex=_ok("docs/cli.md"),
        gemini=_gap(277),
        opencode=_ok("docs/cli.md"),
        deepcode=_gap(277),
        aider=_gap(277),
        implemented=lambda rt: bool(rt.approve_keys),
    ),
    _row(
        "plan-approval",
        "Plan approval answered",
        "src/agent_backbone/services/agents/launch.py",
        claude=_ok("tests/unit/api/routes/test_api_plans.py"),
        codex=_gap(278),
        gemini=_gap(278),
        opencode=_gap(278),
        deepcode=_gap(278),
        aider=_gap(278),
        implemented=lambda rt: bool(rt.plan_approve_keys),
    ),
    _row(
        "refusal-alert",
        "Alert when an automatic safety check refuses an action",
        "src/agent_backbone/services/jobs/escalation.py",
        claude=_ok("tests/unit/hooks/test_claude_hook.py"),
        codex=_gap(279),
        gemini=_gap(279),
        opencode=_gap(279),
        deepcode=_gap(279),
        aider=_gap(279),
    ),
    _row(
        "browser-group-name",
        "Browser tab group named after the agent",
        "src/agent_backbone/hooks/claude_hook.py",
        claude=_ok("tests/unit/hooks/test_chrome_tab_names.py", "with `backbone chrome install`"),
        codex=_gap(270),
        gemini=_gap(270),
        opencode=_gap(270),
        deepcode=_gap(270),
        aider=_gap(270),
    ),
    _row(
        "exact-resume",
        "Resume the agent's own session",
        "src/agent_backbone/services/agents/launch.py",
        claude=_ok("tests/unit/services/runtimes/test_launch_commands.py"),
        codex=_ok("tests/unit/services/runtimes/test_launch_commands.py"),
        gemini=_ok("tests/unit/services/runtimes/test_launch_commands.py"),
        opencode=_ok("tests/unit/services/runtimes/test_launch_commands.py"),
        deepcode=_gap(280, "resumes the directory's latest session"),
        aider=_gap(280),
        shell=_na("a plain shell has no conversation to resume"),
        implemented=lambda rt: rt.supports_exact_resume,
    ),
    _row(
        "unattended",
        "No-approval mode (unattended)",
        "src/agent_backbone/services/runtimes/base.py",
        claude=_ok("tests/unit/services/runtimes/test_launch_commands.py"),
        codex=_ok("tests/unit/services/runtimes/test_launch_commands.py"),
        gemini=_ok("tests/unit/services/runtimes/test_launch_commands.py"),
        opencode=_ok("tests/unit/services/runtimes/test_launch_commands.py"),
        deepcode=_gap(281),
        aider=_gap(281),
        implemented=lambda rt: rt.unattended_args is not None,
    ),
    _row(
        "bounded-unattended",
        "Writes bounded while unattended",
        "src/agent_backbone/services/runtimes/base.py",
        claude=_gap(285),
        codex=_ok("tests/unit/services/runtimes/test_launch_commands.py", "OS sandbox"),
        gemini=_gap(285),
        opencode=_gap(285),
        deepcode=_gap(285),
        aider=_gap(285),
        implemented=lambda rt: rt.sandboxed,
    ),
    _row(
        "skills",
        "Shared skills linked",
        "src/agent_backbone/skills.py",
        claude=_ok("tests/unit/test_skills.py"),
        codex=_ok("tests/unit/test_skills.py"),
        gemini=_ok("tests/unit/test_skills.py"),
        opencode=_ok("tests/unit/test_skills.py"),
        deepcode=_gap(282),
        aider=_gap(282),
        implemented=lambda rt: bool(rt.skill_dirs),
    ),
    _row(
        "usage",
        "Token usage recorded",
        "src/agent_backbone/usage.py",
        claude=_ok("tests/unit/test_usage_accounting.py"),
        codex=_ok("tests/unit/test_usage_accounting.py"),
        gemini=_gap(283),
        opencode=_ok("tests/unit/test_usage_accounting.py"),
        deepcode=_gap(283),
        aider=_gap(283),
        implemented=lambda rt: rt.usage_supported,
    ),
    _row(
        "transcript-output",
        "agent output from the runtime's own record",
        "src/agent_backbone/services/agents/transcript.py",
        claude=_ok("tests/unit/services/runtimes/test_transcripts.py"),
        codex=_ok("tests/unit/services/runtimes/test_transcripts.py"),
        gemini=_gap(284),
        opencode=_gap(284),
        deepcode=_gap(284),
        aider=_gap(284),
        shell=_na("a plain shell has no model messages; output is its screen"),
        implemented=lambda rt: rt.transcript_supported,
    ),
    _row(
        "auto-review",
        "Automatic permission review (agents.auto_review)",
        "src/agent_backbone/services/runtimes/base.py",
        claude=_unverified(293, "auto mode; equivalence not verified"),
        codex=_ok("tests/unit/services/runtimes/test_launch_commands.py", "--approve-for-me"),
        gemini=_gap(293),
        opencode=_gap(293),
        deepcode=_gap(293),
        aider=_gap(293),
        implemented=lambda rt: bool(rt.auto_review_args),
    ),
    _row(
        "deep-review",
        "Deep review run from and for this runtime",
        "docs/deep-reviews.md",
        claude=_ok("docs/deep-reviews.md"),
        codex=_ok("docs/deep-reviews.md"),
        gemini=_gap(288),
        opencode=_gap(288),
        deepcode=_gap(288),
        aider=_gap(288),
    ),
)


def unavailable(runtime_id: str) -> list[Capability]:
    """Capabilities this runtime does not (yet) provide: gaps and unverified cells."""
    return [cap for cap in CAPABILITIES if cap.cells[runtime_id].status in ("gap", "unverified")]


def _cell_text(cell: Cell) -> str:
    mark = {"supported": "✅", "gap": "❌", "unverified": "?", "n/a": "n/a"}[cell.status]
    parts = [mark]
    if cell.issue is not None:
        parts.append(f"#{cell.issue}")
    if cell.note and cell.status != "n/a":
        parts.append(cell.note)
    return " ".join(parts)


def markdown_table(runtime_ids: tuple[str, ...]) -> str:
    """The contract as the table in ``docs/runtime-capabilities.md``."""
    lines = [
        "| Capability | " + " | ".join(f"`{rid}`" for rid in runtime_ids) + " |",
        "|---|" + "---|" * len(runtime_ids),
    ]
    for cap in CAPABILITIES:
        cells = " | ".join(_cell_text(cap.cells[rid]) for rid in runtime_ids)
        lines.append(f"| {cap.title} | {cells} |")
    return "\n".join(lines)
