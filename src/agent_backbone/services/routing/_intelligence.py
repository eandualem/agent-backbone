"""Session intelligence — can this session receive a message right now?

Priority (first match wins):

1. no tmux session                       -> OFFLINE
2. agent waiting for a human             -> WAITING_FOR_HUMAN
3. agent starting/busy                   -> AGENT_WORKING
4. tmux copy mode                        -> cleared automatically, then continue
5. text typed in the prompt (idle agent) -> HUMAN_TYPING
6. idle, grace period not elapsed        -> SETTLING
7. idle                                  -> READY
8. otherwise                             -> UNKNOWN

The agent-reported state (2, 3) always outranks terminal-only signals.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING

from agent_backbone.services.agents._inference import get_agent_state
from agent_backbone.services.agents.models import WORKING_STATES, AgentState
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile
from agent_backbone.services.runtimes import resolve_runtime
from agent_backbone.services.terminal import capture_pane, clear_copy_mode, list_sessions

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

log = logging.getLogger(__name__)


def _profile_current_issue(agent_state: AgentState, current_issue: int | None) -> int | None:
    if agent_state in (AgentState.BUSY, AgentState.WAITING_FOR_HUMAN):
        return current_issue
    return None


async def get_session_intelligence(
    session_name: str,
    config: BackboneConfig,
    idle_since: float | None = None,
) -> SessionProfile:
    """Derive the delivery condition for a session, with evidence."""
    evidence: list[str] = []

    active = await list_sessions()
    if session_name not in active:
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.OFFLINE,
            evidence=["no tmux session with that name"],
        )

    pane_content = ""
    try:
        pane_content = await capture_pane(session_name)
    except Exception:
        log.debug("Failed to capture pane for '%s' (non-fatal)", session_name)

    rt = await resolve_runtime(session_name, pane_content=pane_content)
    runtime = rt.id
    evidence.append(f"runtime: {runtime}")

    state_snap = await get_agent_state(
        config.state_dir,
        session_name,
        config.agent_state.stale_threshold_seconds,
        runtime_hint=runtime,
        pane_content=pane_content,
    )
    evidence.extend(state_snap.evidence)
    agent_state = state_snap.state

    def profile(intel: SessionIntelligence, *extra: str) -> SessionProfile:
        return SessionProfile(
            session_name=session_name,
            intelligence=intel,
            runtime=runtime,
            agent_state=agent_state,
            reason=state_snap.reason,
            current_issue=_profile_current_issue(agent_state, state_snap.current_issue),
            current_repo=state_snap.current_repo,
            state_source=state_snap.source,
            evidence=evidence + list(extra),
        )

    if agent_state == AgentState.WAITING_FOR_HUMAN:
        return profile(SessionIntelligence.WAITING_FOR_HUMAN)

    if agent_state in WORKING_STATES:
        return profile(SessionIntelligence.AGENT_WORKING)

    # Copy mode is a defect, not a state: clear it and re-read the pane.
    was_in_copy_mode, cleared = await clear_copy_mode(session_name)
    if was_in_copy_mode:
        evidence.append(f"tmux copy mode detected — {'cleared' if cleared else 'could not clear'}")
        if not cleared:
            # A frozen pane swallows pastes; report the session as occupied by
            # a human (copy mode is usually someone scrolling) so the message
            # is queued instead of lost.
            return profile(SessionIntelligence.HUMAN_TYPING, "pane stuck in copy mode")
        with contextlib.suppress(Exception):
            pane_content = await capture_pane(session_name)

    if (
        agent_state == AgentState.IDLE
        and pane_content
        and rt.prompt_has_pending_input(pane_content)
    ):
        return profile(SessionIntelligence.HUMAN_TYPING, "prompt contains typed text")

    if agent_state == AgentState.IDLE:
        if idle_since is not None:
            elapsed = time.monotonic() - idle_since
            if elapsed < config.delivery.grace_period_seconds:
                return profile(
                    SessionIntelligence.SETTLING,
                    f"idle for {elapsed:.1f}s < grace {config.delivery.grace_period_seconds}s",
                )
        return profile(SessionIntelligence.READY, "prompt is empty")

    return profile(SessionIntelligence.UNKNOWN)
