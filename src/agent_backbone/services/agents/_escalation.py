"""Escalation logic: stall detection, offline detection, plan-waiting notification."""

from __future__ import annotations

import logging
import time

from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents._inference import get_agent_state
from agent_backbone.services.agents.models import AgentState
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing._format import (
    format_plan_notification,
    format_stall_notification,
    format_unexpected_offline_notification,
)
from agent_backbone.services.routing._targets import list_open_queue_for_target

log = logging.getLogger(__name__)

# Module-level escalation dedup: (session, event_key) → monotonic timestamp
_escalation_dedup: dict[tuple[str, str], float] = {}

# Module-level plan notification dedup: (session, source_ref) → monotonic timestamp
_plan_notify_dedup: dict[tuple[str, str], float] = {}
_PLAN_NOTIFY_DEDUP_SECONDS = 1800


async def safe_deliver(*args, **kwargs):
    """Lazy proxy to avoid importing routing delivery during module import."""
    from agent_backbone.services.routing._delivery import safe_deliver as _safe_deliver

    return await _safe_deliver(*args, **kwargs)


class TelegramService:
    """Lazy proxy to avoid importing telegram service during module import."""

    @staticmethod
    async def send_notification(*args, **kwargs):
        from agent_backbone.services.telegram.interface import TelegramService as _TelegramService

        return await _TelegramService.send_notification(*args, **kwargs)


def _should_escalate(session: str, event_key: str, dedup_seconds: int) -> bool:
    """Check if an escalation should fire, with in-memory dedup.

    Returns True if no recent escalation for this (session, event_key) and
    records the escalation timestamp.
    """
    key = (session, event_key)
    now = time.monotonic()

    expired = [k for k, t in _escalation_dedup.items() if now - t > dedup_seconds]
    for k in expired:
        del _escalation_dedup[k]

    if key in _escalation_dedup:
        return False

    _escalation_dedup[key] = now
    return True


def _plan_notification_source_ref(
    *,
    channel: str,
    recipient: str,
    plan_file: str,
    plan_title: str,
    plan_timestamp: float,
) -> str:
    """Stable identity for one plan-notification delivery target."""
    plan_identity = plan_file or plan_title or "<untitled>"
    return f"{channel}:{recipient}:{plan_timestamp:.6f}:{plan_identity}"


def _plan_notification_already_sent(session_name: str, source_ref: str) -> bool:
    now = time.monotonic()
    expired = [k for k, t in _plan_notify_dedup.items() if now - t > _PLAN_NOTIFY_DEDUP_SECONDS]
    for k in expired:
        del _plan_notify_dedup[k]
    return (session_name, source_ref) in _plan_notify_dedup


def _record_plan_notification(session_name: str, source_ref: str) -> None:
    _plan_notify_dedup[(session_name, source_ref)] = time.monotonic()


def _escalation_session(config: BackboneConfig, source_session: str) -> str | None:
    """The agent that should receive escalations about ``source_session``."""
    target = config.escalation.target
    if not target or target == source_session or target not in config.agents:
        return None
    return target


async def _pending_count_for_agent(name: str, config: BackboneConfig, gh: object) -> int:
    """Count pending work for a configured agent."""
    if gh is None:
        return 0
    return len(await list_open_queue_for_target(config, name, gh))


async def check_for_stalls(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB
) -> list[dict]:
    """Detect stalled agents — those processing an issue beyond the threshold.

    Returns a list of stall records: {entity, session, issue_number, duration_minutes}.
    """
    stalls: list[dict] = []
    threshold = config.escalation.stall_threshold_seconds
    state_path = config.state_dir
    stale_threshold = config.agent_state.stale_threshold_seconds

    for name in config.agents.names:
        if name not in active_sessions:
            continue

        snapshot = await get_agent_state(state_path, name, stale_threshold)

        # Only PROCESSING_ISSUE and BUSY can stall (PLAN_WAITING is human-blocked)
        if snapshot.state not in (AgentState.PROCESSING_ISSUE, AgentState.BUSY):
            continue
        if snapshot.current_issue is None:
            continue
        if snapshot.timestamp <= 0:
            continue

        duration = time.time() - snapshot.timestamp
        if duration >= threshold:
            stalls.append(
                {
                    "entity": name,
                    "session": name,
                    "issue_number": snapshot.current_issue,
                    "duration_minutes": int(duration / 60),
                }
            )

    # Persist current states to DB for offline detection
    try:
        for name in config.agents.names:
            if name not in active_sessions:
                continue
            snapshot = await get_agent_state(state_path, name, stale_threshold)
            await db.set_agent_state(
                session_name=name,
                state=snapshot.state.value,
                current_issue=snapshot.current_issue,
                entity=name,
            )
    except Exception:
        log.exception("Failed to persist agent states (non-fatal)")

    return stalls


async def check_for_unexpected_offline(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB, gh: object
) -> list[dict]:
    """Detect agents that were previously tracked but are now offline.

    Compares active sessions against persisted agent_states. An agent is
    flagged if it was in a non-unknown state but its session is gone.
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

        if state == "unknown":
            continue
        if session_name in active_sessions:
            continue
        if session_name not in config.agents:
            continue

        pending_count = 0
        try:
            pending_count = await _pending_count_for_agent(session_name, config, gh)
        except Exception:
            log.exception("Failed to count pending issues for %s", session_name)

        offline.append(
            {
                "entity": session_name,
                "session": session_name,
                "pending_count": pending_count,
            }
        )

    return offline


async def handle_stalls(config: BackboneConfig, active_sessions: set[str], db: BackboneDB) -> None:
    """Detect stalled agents and escalate to the configured target."""
    stalls = await check_for_stalls(config, active_sessions, db)

    for stall in stalls:
        event_key = f"stall:{stall['issue_number']}"
        if not _should_escalate(stall["session"], event_key, config.escalation.dedup_seconds):
            continue
        escalation_session = _escalation_session(config, stall["session"])
        if escalation_session and escalation_session in active_sessions:
            msg = format_stall_notification(
                stall["session"],
                stall["issue_number"] or 0,
                stall["duration_minutes"],
                stall["entity"],
            )
            await safe_deliver(escalation_session, msg, config, priority=True)
        log.warning(
            "Stall detected: %s on #%s (%dm)",
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

    for agent in offline_agents:
        if _should_escalate(agent["session"], "offline", config.escalation.dedup_seconds):
            escalation_session = _escalation_session(config, agent["session"])
            if escalation_session and escalation_session in active_sessions:
                msg = format_unexpected_offline_notification(
                    agent["session"],
                    agent["entity"],
                    agent["pending_count"],
                )
                await safe_deliver(escalation_session, msg, config, priority=True)
            log.warning("Agent offline unexpectedly: %s", agent["entity"])

        # Mark agent as "unknown" so the next cycle doesn't re-detect the same event.
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


async def check_plan_waiting(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB | None = None
) -> None:
    """Detect plan-waiting agents and notify Telegram + the escalation target."""
    del db  # kept for call-site compatibility; dedup is in-memory
    state_path = config.state_dir
    stale_threshold = config.agent_state.stale_threshold_seconds

    notification_chat_id = config.telegram.notification_chat_id
    telegram_token = config.telegram_token

    for name in config.agents.names:
        if name not in active_sessions:
            continue

        snapshot = await get_agent_state(state_path, name, stale_threshold)
        if snapshot.state != AgentState.PLAN_WAITING:
            continue

        plan_file = snapshot.plan_file or ""
        plan_title = snapshot.plan_title or "Untitled plan"
        plan_timestamp = snapshot.timestamp or 0.0

        # Telegram notification
        if notification_chat_id and telegram_token:
            tg_ref = _plan_notification_source_ref(
                channel="telegram",
                recipient=str(notification_chat_id),
                plan_file=plan_file,
                plan_title=plan_title,
                plan_timestamp=plan_timestamp,
            )
            if not _plan_notification_already_sent(name, tg_ref):
                msg = (
                    f"\U0001f4cb Plan waiting — {name}\n"
                    f"Title: {plan_title}\n\n"
                    f"/viewplan {name}\n"
                    f"/approve {name}"
                )
                sent = await TelegramService.send_notification(
                    telegram_token, notification_chat_id, msg
                )
                if sent:
                    _record_plan_notification(name, tg_ref)
                    log.info("Sent plan-waiting Telegram notification for %s", name)

        # Escalation-target terminal notification
        escalation_session = _escalation_session(config, name)
        if escalation_session and escalation_session in active_sessions:
            orch_ref = _plan_notification_source_ref(
                channel="tmux",
                recipient=escalation_session,
                plan_file=plan_file,
                plan_title=plan_title,
                plan_timestamp=plan_timestamp,
            )
            if not _plan_notification_already_sent(name, orch_ref):
                orch_msg = format_plan_notification(
                    name,
                    name,
                    plan_file,
                    plan_title,
                    issue_number=snapshot.current_issue,
                )
                outcome = await safe_deliver(escalation_session, orch_msg, config, priority=True)
                if outcome == "delivered":
                    _record_plan_notification(name, orch_ref)
                    log.info(
                        "Sent plan notification to %s for %s",
                        escalation_session,
                        name,
                    )
