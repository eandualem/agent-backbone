"""Escalation logic: stall detection, offline detection, plan-waiting notification."""

from __future__ import annotations

import json
import logging
import os
import time

from prefect import task

from agent_backbone.config import REPO_NAME_PATTERN, BackboneConfig
from agent_backbone.services.agents._inference import get_agent_state
from agent_backbone.services.agents.models import AgentState
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing._format import (
    format_plan_notification,
    format_stall_notification,
    format_unexpected_offline_notification,
)

log = logging.getLogger(__name__)

# Module-level escalation dedup: (session, event_key) → monotonic timestamp
_escalation_dedup: dict[tuple[str, str], float] = {}

# Module-level plan notification dedup: (session, plan_file) → monotonic timestamp
_plan_notify_dedup: dict[tuple[str, str], float] = {}
_PLAN_NOTIFY_EVENT = "plan_notification_delivered"


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


async def _plan_notification_already_sent(
    session_name: str,
    source_ref: str,
    dedup_seconds: int,
    db: BackboneDB | None,
) -> bool:
    """Check in-memory and persisted dedup for plan notifications."""
    cache_key = (session_name, source_ref)
    if cache_key in _plan_notify_dedup:
        return True
    if db is None:
        return False

    try:
        recent = await db.has_activity_event(
            session=session_name,
            event=_PLAN_NOTIFY_EVENT,
            source_ref=source_ref,
            since=time.time() - dedup_seconds,
        )
        if recent is True:
            _plan_notify_dedup[cache_key] = time.monotonic()
            return True
    except Exception:
        log.exception("Failed to query persisted plan notification dedup (non-fatal)")
    return False


async def _record_plan_notification(
    session_name: str,
    entity: str,
    source_ref: str,
    payload: dict[str, str],
    db: BackboneDB | None,
) -> None:
    """Record successful plan notification delivery for reload-safe dedup."""
    _plan_notify_dedup[(session_name, source_ref)] = time.monotonic()
    if db is None:
        return

    try:
        await db.record_activity(
            session_name,
            _PLAN_NOTIFY_EVENT,
            json.dumps(payload, sort_keys=True),
            str(time.time()),
            entity=entity,
            source_kind="plan_notification",
            source_ref=source_ref,
        )
    except Exception:
        log.exception("Failed to persist plan notification dedup marker (non-fatal)")


async def _pending_count_for_session(
    entity: str,
    session_name: str,
    config: BackboneConfig,
    gh: object,
    *,
    coding_issues: list | None = None,
) -> int:
    """Count pending work for a monitored session.

    Repo-backed coding sessions can receive both repo-local issues and
    orchestration issues routed through ``for:coding-agent``. Named entities
    only need their direct ``for:{entity}`` queue.
    """
    if session_name not in config.registry.repo_names:
        issues = await gh.list_open_issues(f"for:{entity}")
        return len(issues)

    repo_full_name = f"{config.github.owner}/{session_name}"
    pending = len(await gh.list_issues(state="open", repo_full_name=repo_full_name))
    if not coding_issues:
        return pending

    repo_name = session_name.casefold()
    return pending + sum(
        1
        for issue in coding_issues
        if (
            (match := REPO_NAME_PATTERN.match(issue.title or "")) is not None
            and match.group(1).split("/", 1)[-1].casefold() == repo_name
        )
    )


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

    tracked_sessions = config.registry.tracked_sessions

    for entity, session_name in tracked_sessions.items():
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
        for entity, session_name in tracked_sessions.items():
            if not session_name or session_name not in active_sessions:
                continue
            snapshot = await get_agent_state(state_path, session_name, stale_threshold)
            await db.set_agent_state(
                session_name=session_name,
                state=snapshot.state.value,
                current_issue=snapshot.current_issue,
                entity=entity,
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
    tracked_by_session = {
        session_name: entity for entity, session_name in config.registry.tracked_sessions.items()
    }
    coding_issues: list | None = None

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

        entity = tracked_by_session.get(session_name)
        if not entity:
            continue

        # Count pending issues for context
        pending_count = 0
        try:
            if session_name in config.registry.repo_names and coding_issues is None:
                coding_issues = await gh.list_open_issues("for:coding-agent")
            pending_count = await _pending_count_for_session(
                entity,
                session_name,
                config,
                gh,
                coding_issues=coding_issues,
            )
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


def _resolve_orchestrator(session_name: str, config: BackboneConfig) -> str | None:
    """Resolve orchestrator for a session.

    Coding agents → org orchestrator, named entities → escalation target.
    """
    orch = config.registry.orchestrator_for_repo(session_name)
    if orch:
        return orch
    if session_name in config.registry.entity_by_session:
        return config.escalation.escalation_target
    return None


async def check_plan_waiting(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB | None = None
) -> None:
    """Detect plan-waiting agents and send Telegram + orchestrator notification."""
    now = time.monotonic()
    plan_dedup_seconds = 1800  # 30 minutes

    # Clean expired entries
    expired = [k for k, t in _plan_notify_dedup.items() if now - t > plan_dedup_seconds]
    for k in expired:
        del _plan_notify_dedup[k]

    state_path = config.agent_state.state_path
    stale_threshold = config.agent_state.stale_threshold_seconds

    notification_chat_id = config.telegram.notification_chat_id
    telegram_token = os.environ.get("TELEGRAM_TOKEN", "")

    for entity, session_name in config.registry.tracked_sessions.items():
        if not session_name or session_name not in active_sessions:
            continue

        snapshot = await get_agent_state(state_path, session_name, stale_threshold)
        if snapshot.state != AgentState.PLAN_WAITING:
            continue

        plan_file = snapshot.plan_file or ""
        plan_title = snapshot.plan_title or "Untitled plan"

        # Telegram notification
        plan_timestamp = snapshot.timestamp or 0.0

        tg_source_ref = _plan_notification_source_ref(
            channel="telegram",
            recipient=str(notification_chat_id),
            plan_file=plan_file,
            plan_title=plan_title,
            plan_timestamp=plan_timestamp,
        )
        if (
            notification_chat_id
            and telegram_token
            and not await _plan_notification_already_sent(
                session_name, tg_source_ref, plan_dedup_seconds, db
            )
        ):
            msg = (
                f"\U0001f4cb Plan waiting — {entity}\n"
                f"Title: {plan_title}\n\n"
                f"/viewplan {session_name}\n"
                f"/approve {session_name}"
            )
            sent = await TelegramService.send_notification(
                telegram_token, notification_chat_id, msg
            )
            if sent:
                await _record_plan_notification(
                    session_name,
                    entity,
                    tg_source_ref,
                    {
                        "channel": "telegram",
                        "recipient": str(notification_chat_id),
                        "plan_file": plan_file,
                        "plan_title": plan_title,
                    },
                    db,
                )
                log.info("Sent plan-waiting Telegram notification for %s", entity)

        # Orchestrator tmux notification
        orchestrator = _resolve_orchestrator(session_name, config)
        if orchestrator:
            orch_session = config.registry.sessions_map.get(orchestrator)
            if (
                orch_session
                and orch_session in active_sessions
                and not await _plan_notification_already_sent(
                    session_name,
                    _plan_notification_source_ref(
                        channel="tmux",
                        recipient=orch_session,
                        plan_file=plan_file,
                        plan_title=plan_title,
                        plan_timestamp=plan_timestamp,
                    ),
                    plan_dedup_seconds,
                    db,
                )
            ):
                orch_source_ref = _plan_notification_source_ref(
                    channel="tmux",
                    recipient=orch_session,
                    plan_file=plan_file,
                    plan_title=plan_title,
                    plan_timestamp=plan_timestamp,
                )
                orch_msg = format_plan_notification(
                    session_name,
                    entity,
                    plan_file,
                    plan_title,
                    issue_number=snapshot.current_issue,
                )
                outcome = await safe_deliver(orch_session, orch_msg, config, db=db, priority=True)
                if outcome == "delivered":
                    await _record_plan_notification(
                        session_name,
                        entity,
                        orch_source_ref,
                        {
                            "channel": "tmux",
                            "recipient": orch_session,
                            "plan_file": plan_file,
                            "plan_title": plan_title,
                        },
                        db,
                    )
                    log.info(
                        "Sent plan notification to orchestrator %s for %s",
                        orchestrator,
                        entity,
                    )
