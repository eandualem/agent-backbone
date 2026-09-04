"""Agent state reconciliation: hook state first, terminal reading as fallback."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents.models import (
    REASON_PERMISSION,
    REASON_PLAN,
    REASON_QUESTION,
    AgentState,
    StateSnapshot,
)
from agent_backbone.services.runtimes import (
    GENERIC_BUSY_FRAGMENTS,
    UNKNOWN,
    detect_runtime,
    get_runtime,
    sanitize_pane_content,
)
from agent_backbone.services.terminal import capture_pane

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)

STARTING_TRUST_SECONDS = 120.0
"""How long a ``starting`` marker counts as fresh.

``start_agent`` writes it when the session is created; a hook overwrites it
within seconds and ``wait_until_ready`` clears it when the prompt shows. If
neither happened (a ``--no-wait`` start of a runtime without hooks) the
terminal decides after this window — a runtime takes seconds to start, not
the five minutes a hook state is trusted for.
"""


def _fresh_window(snapshot: StateSnapshot, stale_threshold: float) -> float:
    if snapshot.state == AgentState.STARTING:
        return min(stale_threshold, STARTING_TRUST_SECONDS)
    return stale_threshold


def _trust_stale_push(snapshot: StateSnapshot) -> bool:
    """Whether a stale hook snapshot is still worth using when the pane says nothing."""
    if snapshot.state in (AgentState.IDLE, AgentState.BUSY):
        return True
    if snapshot.state == AgentState.WAITING_FOR_HUMAN and snapshot.reason == REASON_PLAN:
        return bool(snapshot.plan_file and Path(snapshot.plan_file).exists())
    return False


def infer_state_from_pane(pane_content: str, runtime_hint: str | None = None) -> StateSnapshot:
    """Infer the agent state from visible terminal output (with evidence)."""
    lines = sanitize_pane_content(pane_content).strip().splitlines()
    if not lines:
        return StateSnapshot(state=AgentState.UNKNOWN, source="pull", evidence=["empty pane"])

    runtime = get_runtime(runtime_hint)
    if runtime is UNKNOWN:
        runtime = detect_runtime(pane_content)

    if runtime.detect_busy(pane_content):
        return StateSnapshot(
            state=AgentState.BUSY,
            source="pull",
            evidence=[f"terminal shows a busy marker ({runtime.id})"],
        )
    if runtime.detect_waiting_for_human(pane_content):
        return _dialog_snapshot(runtime, pane_content)
    if runtime.detect_idle(pane_content):
        return StateSnapshot(
            state=AgentState.IDLE,
            source="pull",
            evidence=[f"terminal shows an empty prompt ({runtime.id})"],
        )

    recent = "\n".join(ln.strip().lower() for ln in lines[-20:] if ln.strip())
    if any(fragment in recent for fragment in GENERIC_BUSY_FRAGMENTS):
        return StateSnapshot(
            state=AgentState.BUSY, source="pull", evidence=["terminal shows thinking/tool output"]
        )

    return StateSnapshot(
        state=AgentState.UNKNOWN,
        source="pull",
        evidence=[f"terminal inconclusive: no prompt, busy or question marker ({runtime.id})"],
    )


def _dialog_snapshot(runtime, pane_content: str, prefix: list[str] | None = None) -> StateSnapshot:
    """``waiting_for_human`` as read from the terminal: a known permission
    prompt, or any dialog recognised by its numbered options."""
    known = runtime.prompt_markers and any(
        marker in sanitize_pane_content(pane_content).lower()[-2000:]
        for marker in runtime.prompt_markers
    )
    if known:
        reason, seen = REASON_PERMISSION, "a permission prompt"
    else:
        reason, seen = REASON_QUESTION, "a dialog with numbered options"
    return StateSnapshot(
        state=AgentState.WAITING_FOR_HUMAN,
        reason=reason,
        source="pull",
        evidence=[*(prefix or []), f"terminal shows {seen} ({runtime.id})"],
    )


async def get_agent_state(
    state_dir: Path,
    session: str,
    stale_threshold: float = 300.0,
    *,
    runtime_hint: str | None = None,
    pane_content: str | None = None,
) -> StateSnapshot:
    """Reconciled agent state.

    A fresh hook-written state (younger than ``stale_threshold``) is
    authoritative: modern CLIs keep their prompt visible while working, so
    the terminal alone cannot tell busy from idle. Stale or missing hook
    state is verified against the terminal. Every snapshot carries the
    evidence it was built from.
    """
    push = read_state_file(state_dir, session)
    push_age = (time.time() - push.timestamp) if push else None
    if push:
        stale_threshold = _fresh_window(push, stale_threshold)

    if push and push_age is not None and push_age < stale_threshold:
        via = f", {push.event}" if push.event else ""
        push.evidence = [
            f"hook state '{push.state.value}' written {push_age:.0f}s ago (fresh{via})"
        ]
        if push.reason:
            push.evidence.append(f"reason: {push.reason}")
        if push.state == AgentState.IDLE:
            # The one thing a hook cannot see: a dialog drawn by the runtime
            # itself (Claude Code's resume picker arrives after SessionStart
            # already said idle). A dialog on screen beats the idle claim.
            if pane_content is None:
                pane_content = await capture_pane(session)
            runtime = get_runtime(runtime_hint)
            if runtime is UNKNOWN and pane_content:
                runtime = detect_runtime(pane_content)
            if pane_content and runtime.detect_active_dialog(pane_content):
                dialog = _dialog_snapshot(runtime, pane_content, prefix=push.evidence)
                dialog.timestamp = time.time()
                dialog.current_issue = push.current_issue
                dialog.evidence.append("the dialog on screen beats the hook's idle")
                return dialog
        return push

    if pane_content is None:
        pane_content = await capture_pane(session)
    if pane_content:
        pull = infer_state_from_pane(pane_content, runtime_hint)
        pull.timestamp = time.time()
        if push:
            pull.evidence.insert(
                0, f"hook state '{push.state.value}' is stale ({push_age:.0f}s) — reading terminal"
            )
        else:
            pull.evidence.insert(0, "no hook state file — reading terminal")
        if pull.state != AgentState.UNKNOWN:
            return pull
        if push and _trust_stale_push(push):
            push.evidence = [
                *pull.evidence,
                f"terminal inconclusive; falling back to stale hook state '{push.state.value}'",
            ]
            return push
        return pull

    if push and _trust_stale_push(push):
        push.evidence = [
            f"no terminal output; using stale hook state '{push.state.value}' ({push_age:.0f}s)"
        ]
        return push

    return StateSnapshot(
        state=AgentState.UNKNOWN,
        source="default",
        evidence=["no hook state and no terminal output"],
    )


async def agent_state(config: BackboneConfig, name: str) -> StateSnapshot:
    """``get_agent_state`` with the paths and thresholds taken from the configuration."""
    spec = config.agents.get(name)
    return await get_agent_state(
        config.state_dir,
        name,
        config.timing.stale_threshold_seconds,
        runtime_hint=spec.runtime if spec else None,
    )
