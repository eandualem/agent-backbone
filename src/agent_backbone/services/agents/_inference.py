"""Pull-based state inference from tmux + push/pull reconciliation."""

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
    """Whether a stale push snapshot is still trustworthy enough to reuse."""
    match snapshot.state:
        case AgentState.IDLE | AgentState.STARTING | AgentState.BUSY | AgentState.PROCESSING_ISSUE:
            return True
        case AgentState.PLAN_WAITING:
            return bool(snapshot.plan_file and Path(snapshot.plan_file).exists())
        case AgentState.PERMISSION_WAITING | AgentState.UNKNOWN:
            return False
    return False


def prompt_has_pending_input(pane_content: str) -> bool:
    """Whether the current prompt line contains non-empty buffered input."""
    return _prompt_has_pending_input(pane_content)


def infer_state_from_pane(pane_content: str) -> StateSnapshot:
    """Infer the current session state from terminal output."""
    return _infer_state_from_pane(pane_content)


async def get_agent_state(
    state_dir: Path, session: str, stale_threshold: float = 300.0
) -> StateSnapshot:
    """Get reconciled agent state from push + pull sources.

    Push (state file) is preferred when fresh.
    Stale or missing push state is actively verified from tmux before any
    stale fallback is trusted.
    """
    push = read_state_file(state_dir, session)

    if push and (time.time() - push.timestamp) < stale_threshold:
        return push

    pane_content = await capture_pane(session)
    if pane_content:
        pull = infer_state_from_pane(pane_content)
        pull.timestamp = time.time()
        if pull.state != AgentState.UNKNOWN:
            return pull
        if push and _trust_stale_push(push):
            log.info(
                "Pane inference unknown for %s; falling back to stale push state '%s'",
                session,
                push.state.value,
            )
            return push
        return pull

    if push and _trust_stale_push(push):
        log.info(
            "Using stale push state for %s (age: %.0fs)", session, time.time() - push.timestamp
        )
        return push

    return StateSnapshot(state=AgentState.UNKNOWN, source="default")
