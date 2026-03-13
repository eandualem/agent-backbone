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

# User interaction recency threshold (seconds)
_USER_ACTIVITY_THRESHOLD = 10.0


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
    2. pane_in_mode == "1" AND agent NOT working -> COPY_MODE
    3. Recent client_activity + agent idle -> USER_INTERACTING
    4. Agent plan_waiting -> PLAN_WAITING
    5. Agent processing/busy/starting -> AGENT_WORKING
    6. Agent idle + grace not elapsed -> IDLE_GRACE
    7. Agent idle + grace elapsed (or no idle_since) -> IDLE_READY
    8. Otherwise -> UNKNOWN

    Invariant: Steps 4-5 (plan_waiting, agent_working) MUST take priority
    over tmux-only signals (copy_mode, user_interacting). Any tmux-signal
    step that precedes them must guard against working states.

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

    # Derivation priority chain
    # 2. Copy mode — only when agent is NOT in a working state.
    # A working agent whose pane is in copy/scroll mode must still resolve
    # to AGENT_WORKING, not COPY_MODE (which priority=True can bypass).
    if adapter.detect_copy_mode(tmux_vars, agent_state):
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.COPY_MODE,
            runtime=runtime,
            agent_state=agent_state,
            current_issue=state_snap.current_issue,
            tmux_vars=tmux_vars,
        )

    # 3. User interacting (recent keyboard activity or buffered prompt input)
    client_activity_str = tmux_vars.get("client_activity", "")
    if client_activity_str and agent_state == AgentState.IDLE:
        try:
            client_activity = float(client_activity_str)
            if (time.time() - client_activity) < _USER_ACTIVITY_THRESHOLD:
                return SessionProfile(
                    session_name=session_name,
                    intelligence=SessionIntelligence.USER_INTERACTING,
                    runtime=runtime,
                    agent_state=agent_state,
                    current_issue=state_snap.current_issue,
                    tmux_vars=tmux_vars,
                )
        except (ValueError, TypeError):
            pass

    if agent_state == AgentState.IDLE and pane_content and adapter.prompt_has_pending_input(
        pane_content
    ):
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.USER_INTERACTING,
            runtime=runtime,
            agent_state=agent_state,
            current_issue=state_snap.current_issue,
            tmux_vars=tmux_vars,
        )

    # 4. Plan waiting
    if adapter.detect_plan_waiting(state_snap, pane_content):
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.PLAN_WAITING,
            runtime=runtime,
            agent_state=agent_state,
            current_issue=state_snap.current_issue,
            tmux_vars=tmux_vars,
        )

    # 5. Agent working
    if agent_state in _WORKING_STATES:
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.AGENT_WORKING,
            runtime=runtime,
            agent_state=agent_state,
            current_issue=state_snap.current_issue,
            tmux_vars=tmux_vars,
        )

    # 6-7. Idle with/without grace
    if agent_state == AgentState.IDLE:
        if idle_since is not None:
            elapsed = time.monotonic() - idle_since
            if elapsed < config.session_bridge.grace_period_seconds:
                return SessionProfile(
                    session_name=session_name,
                    intelligence=SessionIntelligence.IDLE_GRACE,
                    runtime=runtime,
                    agent_state=agent_state,
                    current_issue=state_snap.current_issue,
                    tmux_vars=tmux_vars,
                )
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.IDLE_READY,
            runtime=runtime,
            agent_state=agent_state,
            current_issue=state_snap.current_issue,
            tmux_vars=tmux_vars,
        )

    # 8. Unknown
    return SessionProfile(
        session_name=session_name,
        intelligence=SessionIntelligence.UNKNOWN,
        runtime=runtime,
        agent_state=agent_state,
        current_issue=state_snap.current_issue,
        tmux_vars=tmux_vars,
    )
