"""Session bridge — composite intelligence, unified resolution, safe delivery.

Combines tmux format variables (pane_in_mode, client_activity) with agent
state files to produce a single SessionIntelligence enum. Provides unified
session resolution and state-aware delivery with SQLite-backed queuing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum

from src.agent_state import AgentState, get_agent_state
from src.config import REPO_NAME_PATTERN, BackboneConfig
from src.tmux import (
    SESSION_FORMAT_STR,
    list_sessions,
    query_format_vars,
    send_message,
    session_exists,
)

log = logging.getLogger(__name__)


class SessionIntelligence(StrEnum):
    """Composite session state derived from tmux vars + agent state."""

    IDLE_READY = "idle_ready"
    IDLE_GRACE = "idle_grace"
    COPY_MODE = "copy_mode"
    USER_INTERACTING = "user_interacting"
    AGENT_WORKING = "agent_working"
    PLAN_WAITING = "plan_waiting"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class SessionProfile:
    """Point-in-time composite session state."""

    session_name: str
    intelligence: SessionIntelligence
    agent_state: AgentState = AgentState.UNKNOWN
    tmux_vars: dict[str, str] = field(default_factory=dict)


# Agent states considered "working" — not available for new deliveries
_WORKING_STATES = frozenset({AgentState.PROCESSING_ISSUE, AgentState.BUSY, AgentState.STARTING})

# User interaction recency threshold (seconds)
_USER_ACTIVITY_THRESHOLD = 10.0


async def get_session_intelligence(
    session_name: str,
    config: BackboneConfig,
    idle_since: float | None = None,
) -> SessionProfile:
    """Get composite session intelligence for a session.

    Combines tmux format vars with agent state to produce a single
    SessionIntelligence value. Derivation priority (first match wins):

    1. Session not active → OFFLINE
    2. pane_in_mode == "1" → COPY_MODE
    3. Recent client_activity + agent idle → USER_INTERACTING
    4. Agent plan_waiting → PLAN_WAITING
    5. Agent processing/busy/starting → AGENT_WORKING
    6. Agent idle + grace not elapsed → IDLE_GRACE
    7. Agent idle + grace elapsed (or no idle_since) → IDLE_READY
    8. Otherwise → UNKNOWN

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


async def resolve_entity_session(
    target: str,
    config: BackboneConfig,
    issue_title: str = "",
    *,
    use_title_extraction: bool = True,
) -> str | None:
    """Resolve a target entity to a tmux session name.

    Unified resolution logic replacing duplicated code in dispatcher and lifecycle.

    Named entities map directly via config. 'coding-agent' extracts repo name
    from issue title (when use_title_extraction=True) and checks session existence,
    falling back to config. Entities in the skip set return None.

    Args:
        target: entity name (e.g. "ike", "coding-agent").
        config: backbone configuration.
        issue_title: issue title for repo name extraction.
        use_title_extraction: if False, skip title parsing for coding-agent
            (used by lifecycle where title format differs).
    """
    # Skip set
    if target in config.entities.skip:
        return None

    # Named entity direct mapping
    if target in config.entities.sessions:
        return config.entities.sessions[target]

    # Coding agent resolution
    if target == "coding-agent":
        if use_title_extraction and issue_title:
            match = REPO_NAME_PATTERN.match(issue_title)
            if match:
                repo_name = match.group(1)
                last_segment = repo_name.rsplit("/", 1)[-1] if "/" in repo_name else repo_name
                candidates = list(
                    dict.fromkeys(
                        [repo_name, repo_name.lower(), last_segment, last_segment.lower()]
                    )
                )
                for candidate in candidates:
                    if await session_exists(candidate):
                        log.info(
                            "Resolved coding-agent → repo session '%s' (from '%s')",
                            candidate,
                            repo_name,
                        )
                        return candidate
                log.info(
                    "No session found for repo '%s' (tried: %s), using fallback",
                    repo_name,
                    candidates,
                )
            else:
                log.info("Could not extract repo name from title: %s", issue_title)

        # Fallback
        fallback = config.entities.fallback.get(target)
        if fallback:
            log.info("Routing coding-agent → fallback '%s'", fallback)
            return fallback
        return None

    # Unknown entity
    return None


async def safe_deliver(
    session_name: str,
    message: str,
    config: BackboneConfig,
    *,
    issue_number: int | None = None,
    target_entity: str | None = None,
    flow_name: str = "",
    priority: bool = False,
) -> str:
    """Deliver a message with composite state pre-checks and optional queuing.

    Checks session intelligence before delivering. Non-deliverable states
    enqueue to SQLite when issue_number + target_entity are provided.

    Args:
        session_name: target tmux session.
        message: notification text to deliver.
        config: backbone configuration.
        issue_number: issue number for queue tracking (optional).
        target_entity: entity name for queue tracking (optional).
        flow_name: originating flow for logging/tracking.
        priority: if True, bypass COPY_MODE and USER_INTERACTING checks.

    Returns:
        Outcome string: "delivered", "offline", "copy_mode", "user_interacting",
        "agent_working", "plan_waiting", "grace_period", "delivery_failed".
    """
    # Get composite state (safe_deliver skips grace — passes idle_since=None)
    profile = await get_session_intelligence(session_name, config)

    intelligence = profile.intelligence

    # Determine deliverability
    if intelligence == SessionIntelligence.OFFLINE:
        await _maybe_enqueue(session_name, message, issue_number, target_entity, flow_name, config)
        return "offline"

    if intelligence == SessionIntelligence.PLAN_WAITING:
        return "plan_waiting"

    if intelligence == SessionIntelligence.AGENT_WORKING:
        return "agent_working"

    if intelligence == SessionIntelligence.IDLE_GRACE:
        return "grace_period"

    if intelligence == SessionIntelligence.COPY_MODE and not priority:
        return "copy_mode"

    if intelligence == SessionIntelligence.USER_INTERACTING and not priority:
        return "user_interacting"

    # Deliverable: IDLE_READY, UNKNOWN, or priority-bypassed COPY_MODE/USER_INTERACTING
    if await send_message(session_name, message):
        return "delivered"

    # Delivery failed
    await _maybe_enqueue(session_name, message, issue_number, target_entity, flow_name, config)
    return "delivery_failed"


async def _maybe_enqueue(
    session_name: str,
    message: str,
    issue_number: int | None,
    target_entity: str | None,
    flow_name: str,
    config: BackboneConfig,
) -> None:
    """Enqueue a message to SQLite if tracking info is provided."""
    if issue_number is None or target_entity is None:
        return
    try:
        from src.persistence import BackboneDB

        async with BackboneDB(str(config.delivery.db_file)) as db:
            await db.enqueue_message(
                session_name=session_name,
                message=message,
                issue_number=issue_number,
                target_entity=target_entity,
                flow_name=flow_name,
            )
        log.info(
            "Enqueued message for %s (#%d) via %s",
            session_name,
            issue_number,
            flow_name or "unknown",
        )
    except Exception:
        log.warning("Failed to enqueue message for %s (non-fatal)", session_name)
