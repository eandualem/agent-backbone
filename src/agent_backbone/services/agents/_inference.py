"""Agent state reconciliation: hook state first, terminal reading as fallback."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents.models import AgentState, StateSnapshot
from agent_backbone.services.terminal._adapters import (
    infer_state_from_pane as _infer_state_from_pane,
)
from agent_backbone.services.terminal._adapters import (
    prompt_has_pending_input as _prompt_has_pending_input,
)
from agent_backbone.services.terminal._core import capture_pane

log = logging.getLogger(__name__)


def _trust_stale_push(snapshot: StateSnapshot) -> bool:
    """Whether a stale hook snapshot is still worth using when the pane says nothing."""
    if snapshot.state in (AgentState.IDLE, AgentState.STARTING, AgentState.BUSY):
        return True
    if snapshot.state == AgentState.WAITING_FOR_HUMAN and snapshot.reason == "plan":
        return bool(snapshot.plan_file and Path(snapshot.plan_file).exists())
    return False


def prompt_has_pending_input(pane_content: str) -> bool:
    return _prompt_has_pending_input(pane_content)


def infer_state_from_pane(pane_content: str, runtime_hint: str | None = None) -> StateSnapshot:
    return _infer_state_from_pane(pane_content, runtime_hint)


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

    if push and push_age is not None and push_age < stale_threshold:
        push.evidence = [f"hook state '{push.state.value}' written {push_age:.0f}s ago (fresh)"]
        if push.reason:
            push.evidence.append(f"reason: {push.reason}")
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
            push.evidence = pull.evidence + [
                f"terminal inconclusive; falling back to stale hook state '{push.state.value}'"
            ]
            return push
        return pull

    if push and push_age is not None and push_age < stale_threshold:
        return push

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
