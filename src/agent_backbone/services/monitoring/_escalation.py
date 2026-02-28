"""Escalation logic: stall detection, offline detection, plan-waiting notification."""

from __future__ import annotations

import logging
import os
import time

from prefect import task

from agent_backbone.config import BackboneConfig
from agent_backbone.services.delivery import safe_deliver
from agent_backbone.services.notifications import (
    format_stall_notification,
    format_unexpected_offline_notification,
)
from agent_backbone.services.persistence import BackboneDB
from agent_backbone.services.state import AgentState, get_agent_state
from agent_backbone.services.telegram import TelegramService

log = logging.getLogger(__name__)

# Module-level escalation dedup: (session, event_key) → monotonic timestamp
_escalation_dedup: dict[tuple[str, str], float] = {}

# Module-level plan notification dedup: (session, plan_file) → monotonic timestamp
_plan_notify_dedup: dict[tuple[str, str], float] = {}


def _should_escalate(session: str, event_key: str, dedup_seconds: int) -> bool:
    """Check if an escalation should fire, with in-memory dedup.

    Returns True if no recent escalation for this (session, event_key).
    Records the escalation timestamp on True.
    """
    key = (session, event_key)
    now = time.monotonic()

    # Clean expired entries
    expired = [k for k, t in _escalation_dedup.items() if now - t > dedup_seconds]
    for k in expired:
        del _escalation_dedup[k]

    if key in _escalation_dedup:
        return False

    _escalation_dedup[key] = now
    return True


@task
async def check_for_stalls(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB
) -> list[dict]:
    """Detect stalled agents — those processing an issue beyond the threshold.

    Returns a list of stall records: {entity, session, issue_number, duration_minutes}.
    """
    stalls: list[dict] = []
    threshold = config.escalation.stall_threshold_seconds
    state_path = config.agent_state.state_path
    stale_threshold = config.agent_state.stale_threshold_seconds

    for entity in config.registry.all_entities:
        session_name = config.registry.sessions_map.get(entity)
        if not session_name or session_name not in active_sessions:
            continue

        snapshot = await get_agent_state(state_path, session_name, stale_threshold)

        # Only PROCESSING_ISSUE and BUSY can be stalled (PLAN_WAITING is human-blocked, not stalled)
        if snapshot.state not in (AgentState.PROCESSING_ISSUE, AgentState.BUSY):
            continue

        # No assigned issue — agent is busy with housekeeping, not stalled
        if snapshot.current_issue is None:
            continue

        # Use state timestamp (last update), not session start time
        if snapshot.timestamp <= 0:
            continue

        duration = time.time() - snapshot.timestamp
        if duration >= threshold:
            stalls.append(
                {
                    "entity": entity,
                    "session": session_name,
                    "issue_number": snapshot.current_issue,
                    "duration_minutes": int(duration / 60),
                }
            )

    # Persist current states to DB for offline detection
    try:
        for entity in config.registry.all_entities:
            session_name = config.registry.sessions_map.get(entity)
            if not session_name or session_name not in active_sessions:
                continue
            snapshot = await get_agent_state(state_path, session_name, stale_threshold)
            await db.set_agent_state(
                session_name=session_name,
                state=snapshot.state.value,
                current_issue=snapshot.current_issue,
            )
    except Exception:
        log.exception("Failed to persist agent states (non-fatal)")

    return stalls


@task
async def check_for_unexpected_offline(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB, gh: object
) -> list[dict]:
    """Detect agents that were previously tracked but are now offline.

    Compares active sessions against persisted agent_states. An agent is
    flagged if it was in a non-unknown state but its session is gone.

    Returns list of offline records: {entity, session, pending_count}.
    """
    offline: list[dict] = []

    try:
        known_states = await db.get_all_agent_states()
    except Exception:
        log.exception("Failed to read agent states for offline check")
        return offline

    for record in known_states:
        session_name = record["session_name"]
        state = record.get("state", "unknown")

        # Skip sessions that were already unknown
        if state == "unknown":
            continue

        # If session is still active, no problem
        if session_name in active_sessions:
            continue

        # Map session back to entity
        entity = None
        for ent, sess in config.registry.sessions_map.items():
            if sess == session_name:
                entity = ent
                break

        if not entity:
            continue

        # Count pending issues for context
        pending_count = 0
        try:
            issues = await gh.list_open_issues(f"for:{entity}")
            pending_count = len(issues)
        except Exception:
            log.exception("Failed to count pending issues for %s", entity)

        offline.append(
            {
                "entity": entity,
                "session": session_name,
                "pending_count": pending_count,
            }
        )

    return offline


async def handle_stalls(config: BackboneConfig, active_sessions: set[str], db: BackboneDB) -> None:
    """Detect stalled agents and escalate to the configured target."""
    stalls = await check_for_stalls(config, active_sessions, db)
    escalation_target = config.escalation.escalation_target
    escalation_session = config.registry.sessions_map.get(escalation_target)

    for stall in stalls:
        event_key = f"stall:{stall['issue_number']}"
        if _should_escalate(
            stall["session"], event_key, config.escalation.escalation_dedup_seconds
        ):
            if escalation_session and escalation_session in active_sessions:
                msg = format_stall_notification(
                    stall["session"],
                    stall["issue_number"] or 0,
                    stall["duration_minutes"],
                    stall["entity"],
                )
                await safe_deliver(escalation_session, msg, config, priority=True)
                log.warning(
                    "Escalated stall: %s on #%s (%dm)",
                    stall["entity"],
                    stall["issue_number"],
                    stall["duration_minutes"],
                )


async def handle_offline(
    config: BackboneConfig,
    active_sessions: set[str],
    db: BackboneDB,
    gh: object,
) -> None:
    """Detect unexpectedly offline agents and escalate."""
    offline_agents = await check_for_unexpected_offline(config, active_sessions, db, gh)
    escalation_target = config.escalation.escalation_target
    escalation_session = config.registry.sessions_map.get(escalation_target)

    for agent in offline_agents:
        event_key = "offline"
        if _should_escalate(
            agent["session"], event_key, config.escalation.escalation_dedup_seconds
        ):
            if escalation_session and escalation_session in active_sessions:
                msg = format_unexpected_offline_notification(
                    agent["session"],
                    agent["entity"],
                    agent["pending_count"],
                )
                await safe_deliver(escalation_session, msg, config, priority=True)
                log.warning("Escalated offline: %s", agent["entity"])

            # Mark agent as "unknown" in DB so the next cycle doesn't
            # re-detect the same offline event.
            try:
                await db.set_agent_state(
                    session_name=agent["session"],
                    state="unknown",
                    current_issue=None,
                )
            except Exception:
                log.exception(
                    "Failed to clear DB state for offline agent %s (non-fatal)",
                    agent["session"],
                )


async def check_plan_waiting(config: BackboneConfig, active_sessions: set[str]) -> None:
    """Detect plan-waiting agents and send Telegram notification."""
    notification_chat_id = config.telegram.notification_chat_id
    telegram_token = os.environ.get("TELEGRAM_TOKEN", "")

    if not notification_chat_id or not telegram_token:
        return

    now = time.monotonic()
    plan_dedup_seconds = 1800  # 30 minutes

    # Clean expired entries
    expired = [k for k, t in _plan_notify_dedup.items() if now - t > plan_dedup_seconds]
    for k in expired:
        del _plan_notify_dedup[k]

    state_path = config.agent_state.state_path
    stale_threshold = config.agent_state.stale_threshold_seconds

    for entity in config.registry.all_entities:
        session_name = config.registry.sessions_map.get(entity)
        if not session_name or session_name not in active_sessions:
            continue

        snapshot = await get_agent_state(state_path, session_name, stale_threshold)
        if snapshot.state != AgentState.PLAN_WAITING:
            continue

        plan_file = snapshot.plan_file or ""
        dedup_key = (session_name, plan_file)
        if dedup_key in _plan_notify_dedup:
            continue

        plan_title = snapshot.plan_title or "Untitled plan"
        msg = (
            f"\U0001f4cb Plan waiting — {entity}\n"
            f"Title: {plan_title}\n\n"
            f"/viewplan {session_name}\n"
            f"/approve {session_name}"
        )
        sent = await TelegramService.send_notification(telegram_token, notification_chat_id, msg)
        if sent:
            _plan_notify_dedup[dedup_key] = now
            log.info("Sent plan-waiting notification for %s", entity)
