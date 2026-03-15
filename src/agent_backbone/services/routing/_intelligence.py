"""Session intelligence — composite state from tmux vars + agent state."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig

from agent_backbone.services.agents._inference import get_agent_state
from agent_backbone.services.agents.models import AgentState
from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile
from agent_backbone.services.terminal import (
    SESSION_FORMAT_STR,
    capture_pane,
    get_terminal_adapter,
    list_sessions,
    query_format_vars,
    resolve_terminal_runtime,
)

log = logging.getLogger(__name__)


# Agent states considered "working" — not available for new deliveries
_WORKING_STATES = frozenset({AgentState.PROCESSING_ISSUE, AgentState.BUSY, AgentState.STARTING})


def _profile_current_issue(agent_state: AgentState, current_issue: int | None) -> int | None:
    """Only expose current_issue for states where the spec says it is meaningful."""
    if agent_state in (
        AgentState.PROCESSING_ISSUE,
        AgentState.PLAN_WAITING,
        AgentState.PERMISSION_WAITING,
    ):
        return current_issue
    return None


def is_http_target(session_name: str, config: BackboneConfig) -> bool:
    """Check if a session name represents an HTTP delivery target."""
    return session_name == "jarvis" and config.jarvis.enabled


async def get_session_intelligence(
    session_name: str,
    config: BackboneConfig,
    idle_since: float | None = None,
) -> SessionProfile:
    """Get composite session intelligence for a session.

    Combines tmux format vars with agent state to produce a single
    SessionIntelligence value. Derivation priority (first match wins):

    1. Session not active -> OFFLINE
    2. Agent plan_waiting -> PLAN_WAITING
    3. Agent permission_waiting -> PERMISSION_WAITING
    4. Agent processing/busy/starting -> AGENT_WORKING
    5. pane_in_mode == "1" AND agent not busy -> COPY_MODE
    6. Buffered prompt input + agent idle -> USER_INTERACTING
    7. Agent idle + grace not elapsed -> IDLE_GRACE
    8. Agent idle + grace elapsed (or no idle_since) -> IDLE_READY
    9. Otherwise -> UNKNOWN

    Invariant: Steps 2-4 (plan_waiting, permission_waiting, agent_working) MUST take priority
    over tmux-only signals (copy_mode, user_interacting). Any tmux-signal
    step that precedes them must guard against those states.

    Args:
        session_name: tmux session to query.
        config: backbone configuration.
        idle_since: monotonic timestamp when agent became idle (caller-managed).
            If None, grace period check is skipped.
    """
    # Step 1: Check session existence
    active = await list_sessions()
    if session_name not in active:
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.OFFLINE,
            runtime="unknown",
            current_issue=None,
        )

    # Step 2: Query tmux format vars (non-fatal on failure)
    tmux_vars: dict[str, str] = {}
    try:
        tmux_vars = await query_format_vars(session_name, SESSION_FORMAT_STR)
    except Exception:
        log.debug("Failed to query tmux vars for '%s' (non-fatal)", session_name)

    pane_content = ""
    try:
        pane_content = await capture_pane(session_name)
    except Exception:
        log.debug("Failed to capture pane for '%s' (non-fatal)", session_name)

    runtime = (
        await resolve_terminal_runtime(
            session_name,
            pane_content=pane_content,
        )
    ).value
    adapter = get_terminal_adapter(runtime)

    # Step 3: Get agent state via push/pull reconciliation
    state_snap = await get_agent_state(
        config.agent_state.state_path,
        session_name,
        config.agent_state.stale_threshold_seconds,
    )
    agent_state = state_snap.state
    current_issue = _profile_current_issue(agent_state, state_snap.current_issue)

    # Derivation priority chain
    # 2. Plan waiting
    if adapter.detect_plan_waiting(state_snap, pane_content):
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.PLAN_WAITING,
            runtime=runtime,
            agent_state=agent_state,
            current_issue=current_issue,
            tmux_vars=tmux_vars,
        )

    # 3. Permission waiting
    if agent_state == AgentState.PERMISSION_WAITING:
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.PERMISSION_WAITING,
            runtime=runtime,
            agent_state=agent_state,
            current_issue=current_issue,
            tmux_vars=tmux_vars,
        )

    # 4. Agent working
    if agent_state in _WORKING_STATES:
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.AGENT_WORKING,
            runtime=runtime,
            agent_state=agent_state,
            current_issue=current_issue,
            tmux_vars=tmux_vars,
        )

    # 5. Copy mode — tmux signal only after higher-priority agent states.
    if adapter.detect_copy_mode(tmux_vars, agent_state):
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.COPY_MODE,
            runtime=runtime,
            agent_state=agent_state,
            current_issue=current_issue,
            tmux_vars=tmux_vars,
        )

    # 6. User interacting — only buffered prompt input counts.
    if (
        agent_state == AgentState.IDLE
        and pane_content
        and adapter.prompt_has_pending_input(pane_content)
    ):
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.USER_INTERACTING,
            runtime=runtime,
            agent_state=agent_state,
            current_issue=current_issue,
            tmux_vars=tmux_vars,
        )

    # 7-8. Idle with/without grace
    if agent_state == AgentState.IDLE:
        if idle_since is not None:
            elapsed = time.monotonic() - idle_since
            if elapsed < config.session_bridge.grace_period_seconds:
                return SessionProfile(
                    session_name=session_name,
                    intelligence=SessionIntelligence.IDLE_GRACE,
                    runtime=runtime,
                    agent_state=agent_state,
                    current_issue=current_issue,
                    tmux_vars=tmux_vars,
                )
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.IDLE_READY,
            runtime=runtime,
            agent_state=agent_state,
            current_issue=current_issue,
            tmux_vars=tmux_vars,
        )

    # 9. Unknown
    return SessionProfile(
        session_name=session_name,
        intelligence=SessionIntelligence.UNKNOWN,
        runtime=runtime,
        agent_state=agent_state,
        current_issue=current_issue,
        tmux_vars=tmux_vars,
    )
