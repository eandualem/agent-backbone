"""Escalation: stalls, dead sessions, and plans waiting for a human.

Nothing here restarts anything. The backbone reports; people (or the
escalation-target agent) decide.
"""

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

_escalation_dedup: dict[tuple[str, str], float] = {}
_plan_notify_dedup: dict[tuple[str, str], float] = {}
_PLAN_NOTIFY_DEDUP_SECONDS = 1800


async def safe_deliver(*args, **kwargs):
    """Lazy proxy to avoid importing routing delivery during module import."""
    from agent_backbone.services.routing._delivery import safe_deliver as _safe_deliver

    return await _safe_deliver(*args, **kwargs)


def outcome_queues(outcome: str, kind: str) -> bool:
    """Lazy proxy to avoid importing routing delivery during module import."""
    from agent_backbone.services.routing._delivery import outcome_queues as _outcome_queues

    return _outcome_queues(outcome, kind)


async def _notify_humans(config: BackboneConfig, text: str, *, agent: str | None = None) -> bool:
    """Alert the humans on every configured integration (lazy: integrations sit above agents)."""
    from agent_backbone.services.integrations import notify_humans

    return await notify_humans(config, text, agent=agent)


def _should_escalate(session: str, event_key: str, dedup_seconds: int) -> bool:
    key = (session, event_key)
    now = time.monotonic()
    for stale in [k for k, t in _escalation_dedup.items() if now - t > dedup_seconds]:
        del _escalation_dedup[stale]
    if key in _escalation_dedup:
        return False
    _escalation_dedup[key] = now
    return True


def _plan_notification_source_ref(
    *, channel: str, recipient: str, plan_file: str, plan_title: str, plan_timestamp: float
) -> str:
    plan_identity = plan_file or plan_title or "<untitled>"
    return f"{channel}:{recipient}:{plan_timestamp:.6f}:{plan_identity}"


def _plan_notification_already_sent(session_name: str, source_ref: str) -> bool:
    now = time.monotonic()
    for stale in [k for k, t in _plan_notify_dedup.items() if now - t > _PLAN_NOTIFY_DEDUP_SECONDS]:
        del _plan_notify_dedup[stale]
    return (session_name, source_ref) in _plan_notify_dedup


def _record_plan_notification(session_name: str, source_ref: str) -> None:
    _plan_notify_dedup[(session_name, source_ref)] = time.monotonic()


def _escalation_session(config: BackboneConfig, source_session: str) -> str | None:
    target = config.escalation.target
    if not target or target == source_session or target not in config.agents:
        return None
    return target


async def _pending_count_for_agent(name: str, config: BackboneConfig, gh: object) -> int:
    if gh is None:
        return 0
    return len(await list_open_queue_for_target(config, name, gh))


async def check_for_stalls(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB
) -> list[dict]:
    """Agents busy on one issue for longer than ``timing.stall_threshold_seconds``."""
    stalls: list[dict] = []
    threshold = config.escalation.stall_threshold_seconds
    state_path = config.state_dir
    stale_threshold = config.agent_state.stale_threshold_seconds

    for name in config.agents.names:
        if name not in active_sessions:
            continue
        snapshot = await get_agent_state(state_path, name, stale_threshold)
        try:
            await db.set_agent_state(session_name=name, **snapshot.db_fields())
        except Exception:
            log.exception("Failed to persist agent state for %s (non-fatal)", name)

        if snapshot.state != AgentState.BUSY or snapshot.current_issue is None:
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
                    "repo": snapshot.current_repo or "",
                    "duration_minutes": int(duration / 60),
                }
            )
    return stalls


async def check_for_unexpected_offline(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB, gh: object
) -> list[dict]:
    """Known agents whose last recorded state was live but whose session is gone."""
    offline: list[dict] = []
    try:
        known_states = await db.get_all_agent_states()
    except Exception:
        log.exception("Failed to read agent states for offline check")
        return offline

    for record in known_states:
        session_name = record["session_name"]
        if record.get("state", "unknown") == "unknown":
            continue
        if session_name in active_sessions or session_name not in config.agents:
            continue
        pending_count = 0
        try:
            pending_count = await _pending_count_for_agent(session_name, config, gh)
        except Exception:
            log.exception("Failed to count pending issues for %s", session_name)
        offline.append(
            {"entity": session_name, "session": session_name, "pending_count": pending_count}
        )
    return offline


async def handle_stalls(config: BackboneConfig, active_sessions: set[str], db: BackboneDB) -> None:
    """Detect stalled agents and escalate to the configured target."""
    for stall in await check_for_stalls(config, active_sessions, db):
        event_key = f"stall:{stall['repo']}#{stall['issue_number']}"
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
            await safe_deliver(
                escalation_session, msg, config, db=db, priority=True, delivery_kind="escalation"
            )
        log.warning(
            "Stall detected: %s on %s#%s (%dm)",
            stall["entity"],
            stall["repo"],
            stall["issue_number"],
            stall["duration_minutes"],
        )


async def handle_offline(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB, gh: object
) -> None:
    """Report dead sessions (never restart them) and clear their recorded state."""
    for agent in await check_for_unexpected_offline(config, active_sessions, db, gh):
        if _should_escalate(agent["session"], "offline", config.escalation.dedup_seconds):
            escalation_session = _escalation_session(config, agent["session"])
            if escalation_session and escalation_session in active_sessions:
                msg = format_unexpected_offline_notification(
                    agent["session"], agent["entity"], agent["pending_count"]
                )
                await safe_deliver(
                    escalation_session,
                    msg,
                    config,
                    db=db,
                    priority=True,
                    delivery_kind="escalation",
                )
            await _notify_humans(
                config,
                f"Agent {agent['entity']} went offline unexpectedly "
                f"({agent['pending_count']} pending). It was not restarted.",
                agent=agent["session"],
            )
            log.warning("Agent offline unexpectedly: %s", agent["entity"])
        try:
            await db.set_agent_state(
                session_name=agent["session"], state="unknown", current_issue=None
            )
        except Exception:
            log.exception(
                "Failed to clear DB state for offline agent %s (non-fatal)", agent["session"]
            )


async def check_plan_waiting(
    config: BackboneConfig, active_sessions: set[str], db: BackboneDB | None = None
) -> None:
    """Tell the humans (every integration) and the escalation target about waiting plans."""
    state_path = config.state_dir
    stale_threshold = config.agent_state.stale_threshold_seconds

    for name in config.agents.names:
        if name not in active_sessions:
            continue
        snapshot = await get_agent_state(state_path, name, stale_threshold)
        if not snapshot.is_plan_waiting:
            continue

        plan_file = snapshot.plan_file or ""
        plan_title = snapshot.plan_title or "Untitled plan"
        plan_timestamp = snapshot.timestamp or 0.0

        human_ref = _plan_notification_source_ref(
            channel="integrations",
            recipient="humans",
            plan_file=plan_file,
            plan_title=plan_title,
            plan_timestamp=plan_timestamp,
        )
        if not _plan_notification_already_sent(name, human_ref):
            msg = (
                f"\U0001f4cb Plan waiting — {name}\nTitle: {plan_title}\n\n"
                f"/viewplan {name}\n/approve {name}"
            )
            if await _notify_humans(config, msg, agent=name):
                _record_plan_notification(name, human_ref)
                log.info("Sent plan-waiting notification for %s", name)

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
                    name, name, plan_file, plan_title, issue_number=snapshot.current_issue
                )
                outcome = await safe_deliver(
                    escalation_session,
                    orch_msg,
                    config,
                    db=db,
                    priority=True,
                    delivery_kind="escalation",
                )
                # A queued notification will reach the target when it frees up,
                # so record it either way and do not enqueue it again next run.
                if outcome == "delivered" or outcome_queues(outcome, "escalation"):
                    _record_plan_notification(name, orch_ref)
                    log.info(
                        "Plan notification for %s -> %s (%s)", name, escalation_session, outcome
                    )
