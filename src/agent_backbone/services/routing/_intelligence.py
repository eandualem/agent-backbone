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
    list_sessions,
    query_format_vars,
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
    2. pane_in_mode == "1" -> COPY_MODE
    3. Recent client_activity + agent idle -> USER_INTERACTING
    4. Agent plan_waiting -> PLAN_WAITING
    5. Agent processing/busy/starting -> AGENT_WORKING
    6. Agent idle + grace not elapsed -> IDLE_GRACE
    7. Agent idle + grace elapsed (or no idle_since) -> IDLE_READY
    8. Otherwise -> UNKNOWN

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
        )

    # Step 2: Query tmux format vars (non-fatal on failure)
    tmux_vars: dict[str, str] = {}
    try:
        tmux_vars = await query_format_vars(session_name, SESSION_FORMAT_STR)
    except Exception:
        log.debug("Failed to query tmux vars for '%s' (non-fatal)", session_name)

    # Step 3: Get agent state via push/pull reconciliation
    state_snap = await get_agent_state(
        config.agent_state.state_path,
        session_name,
        config.agent_state.stale_threshold_seconds,
    )
    agent_state = state_snap.state

    # Derivation priority chain
    # 2. Copy mode
    if tmux_vars.get("pane_in_mode") == "1":
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.COPY_MODE,
            agent_state=agent_state,
            tmux_vars=tmux_vars,
        )

    # 3. User interacting (recent keyboard activity + agent idle)
    client_activity_str = tmux_vars.get("client_activity", "")
    if client_activity_str and agent_state == AgentState.IDLE:
        try:
            client_activity = float(client_activity_str)
            if (time.time() - client_activity) < _USER_ACTIVITY_THRESHOLD:
                return SessionProfile(
                    session_name=session_name,
                    intelligence=SessionIntelligence.USER_INTERACTING,
                    agent_state=agent_state,
                    tmux_vars=tmux_vars,
                )
        except (ValueError, TypeError):
            pass

    # 4. Plan waiting
    if agent_state == AgentState.PLAN_WAITING:
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.PLAN_WAITING,
            agent_state=agent_state,
            tmux_vars=tmux_vars,
        )

    # 5. Agent working
    if agent_state in _WORKING_STATES:
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.AGENT_WORKING,
            agent_state=agent_state,
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
                    agent_state=agent_state,
                    tmux_vars=tmux_vars,
                )
        return SessionProfile(
            session_name=session_name,
            intelligence=SessionIntelligence.IDLE_READY,
            agent_state=agent_state,
            tmux_vars=tmux_vars,
        )

    # 8. Unknown
    return SessionProfile(
        session_name=session_name,
        intelligence=SessionIntelligence.UNKNOWN,
        agent_state=agent_state,
        tmux_vars=tmux_vars,
    )
