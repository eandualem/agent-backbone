"""Safe delivery — state-aware message delivery with optional queuing."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.persistence import BackboneDB

from agent_backbone.services.delivery._intelligence import get_session_intelligence, is_http_target
from agent_backbone.services.delivery.models import SessionIntelligence
from agent_backbone.services.tmux import send_message

log = logging.getLogger(__name__)


async def safe_deliver(
    session_name: str,
    message: str,
    config: BackboneConfig,
    *,
    db: BackboneDB | None = None,
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
    # HTTP delivery targets bypass tmux intelligence entirely
    if is_http_target(session_name, config):
        from agent_backbone.jarvis import inject_message

        if await inject_message(
            config.jarvis.inject_url, message, sessions_url=config.jarvis.sessions_url
        ):
            return "delivered"
        await _maybe_enqueue(session_name, message, issue_number, target_entity, flow_name, db)
        return "delivery_failed"

    # Get composite state (safe_deliver skips grace — passes idle_since=None)
    profile = await get_session_intelligence(session_name, config)

    intelligence = profile.intelligence

    # Determine deliverability
    if intelligence == SessionIntelligence.OFFLINE:
        await _maybe_enqueue(session_name, message, issue_number, target_entity, flow_name, db)
        return "offline"

    if intelligence == SessionIntelligence.PLAN_WAITING:
        return "plan_waiting"

    if intelligence == SessionIntelligence.AGENT_WORKING:
        return "agent_working"

    if intelligence == SessionIntelligence.IDLE_GRACE:
        return "grace_period"

    if intelligence == SessionIntelligence.COPY_MODE and not priority:
        await _maybe_enqueue(session_name, message, issue_number, target_entity, flow_name, db)
        return "copy_mode"

    if intelligence == SessionIntelligence.USER_INTERACTING and not priority:
        await _maybe_enqueue(session_name, message, issue_number, target_entity, flow_name, db)
        return "user_interacting"

    # Deliverable: IDLE_READY, UNKNOWN, or priority-bypassed COPY_MODE/USER_INTERACTING
    if await send_message(session_name, message):
        return "delivered"

    # Delivery failed
    await _maybe_enqueue(session_name, message, issue_number, target_entity, flow_name, db)
    return "delivery_failed"


async def _maybe_enqueue(
    session_name: str,
    message: str,
    issue_number: int | None,
    target_entity: str | None,
    flow_name: str,
    db: BackboneDB | None = None,
) -> None:
    """Enqueue a message to SQLite if tracking info and db are provided."""
    if issue_number is None or target_entity is None:
        return
    if db is None:
        log.debug("No DB provided to _maybe_enqueue — skipping enqueue for %s", session_name)
        return
    try:
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


async def list_sessions_full(config: BackboneConfig) -> list[dict]:
    """List all tmux sessions enriched with intelligence and agent state.

    Combines list_sessions_rich() metadata (name, windows, created, attached)
    with get_session_intelligence() for each session. Returns a list of dicts
    with all rich fields plus 'intelligence' and 'agent_state' string fields.
    """
    from agent_backbone.services.tmux import list_sessions_rich

    sessions = await list_sessions_rich()
    results: list[dict] = []
    for session in sessions:
        name = session["name"]
        profile = await get_session_intelligence(name, config)
        enriched = dict(session)
        enriched["intelligence"] = str(profile.intelligence)
        enriched["agent_state"] = str(profile.agent_state)
        results.append(enriched)
    return results
