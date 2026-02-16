"""Periodic agent monitor: check sessions, deliver pending issues, detect stalls.

Runs on a 60-second interval. For each known entity, checks if their
tmux session is online and delivers pending issues that may have arrived
while the session was down. Also detects stalled and unexpectedly offline
agents, escalating to the configured target.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime

from prefect import flow, task

from src.agent_state import AgentState, get_agent_state, has_commented_on_issue, should_deliver
from src.config import BackboneConfig
from src.github import GitHubClient
from src.models import IssueData
from src.notifications import (
    format_next_issue_notification,
    format_stall_notification,
    format_unexpected_offline_notification,
)
from src.persistence import BackboneDB
from src.telegram import BackboneBot
from src.tmux import list_sessions, send_message

log = logging.getLogger(__name__)

# Module-level escalation dedup: (session, event_key) → monotonic timestamp
_escalation_dedup: dict[tuple[str, str], float] = {}

# Module-level plan notification dedup: (session, plan_file) → monotonic timestamp
_plan_notify_dedup: dict[tuple[str, str], float] = {}

# Concurrency guard — prevents parallel monitor runs on startup burst
_monitor_lock = asyncio.Lock()


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
async def check_pending_issues(config: BackboneConfig, entity: str) -> list[IssueData]:
    """Get all pending open issues for an entity, sorted by priority."""
    label = f"for:{entity}"
    async with GitHubClient(config) as gh:
        return await gh.list_open_issues(label)


@task
async def check_for_stalls(config: BackboneConfig, active_sessions: set[str]) -> list[dict]:
    """Detect stalled agents — those processing an issue beyond the threshold.

    Returns a list of stall records: {entity, session, issue_number, duration_minutes}.
    """
    stalls: list[dict] = []
    threshold = config.escalation.stall_threshold_seconds
    state_path = config.agent_state.state_path
    stale_threshold = config.agent_state.stale_threshold_seconds

    for entity in config.entities.all_entities:
        session_name = config.entities.sessions.get(entity)
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
        async with BackboneDB(str(config.delivery.db_file)) as db:
            for entity in config.entities.all_entities:
                session_name = config.entities.sessions.get(entity)
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
    config: BackboneConfig, active_sessions: set[str]
) -> list[dict]:
    """Detect agents that were previously tracked but are now offline.

    Compares active sessions against persisted agent_states. An agent is
    flagged if it was in a non-unknown state but its session is gone.

    Returns list of offline records: {entity, session, pending_count}.
    """
    offline: list[dict] = []

    try:
        async with BackboneDB(str(config.delivery.db_file)) as db:
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
        for ent, sess in config.entities.sessions.items():
            if sess == session_name:
                entity = ent
                break

        if not entity:
            continue

        # Count pending issues for context
        pending_count = 0
        try:
            async with GitHubClient(config) as gh:
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


@flow(name="agent-monitor")
async def monitor_agents() -> dict:
    """Check all entity sessions and deliver pending issues to online agents.

    Also detects stalled and unexpectedly offline agents, escalating to
    the configured target with dedup.

    Returns a dict mapping entity → action taken.
    """
    # Prevent concurrent runs (Prefect may schedule a burst on startup)
    if _monitor_lock.locked():
        log.info("Monitor already running — skipping concurrent run")
        return {"_skipped": "concurrent_run"}

    async with _monitor_lock:
        return await _monitor_agents_impl()


async def _monitor_agents_impl() -> dict:
    """Inner implementation of monitor_agents, guarded by _monitor_lock."""
    result: dict[str, str] = {}
    config = BackboneConfig.from_toml()

    active_sessions = set(await list_sessions())
    if not active_sessions:
        log.info("No tmux sessions active")
        return result

    # Sync sub-issue dependencies for open issues
    try:
        await _sync_dependencies(config)
    except Exception:
        log.exception("Dependency sync failed (non-fatal)")

    # Detect stalled agents
    try:
        stalls = await check_for_stalls(config, active_sessions)
        escalation_target = config.escalation.escalation_target
        escalation_session = config.entities.sessions.get(escalation_target)

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
                    await send_message(escalation_session, msg)
                    log.warning(
                        "Escalated stall: %s on #%s (%dm)",
                        stall["entity"],
                        stall["issue_number"],
                        stall["duration_minutes"],
                    )
    except Exception:
        log.exception("Stall detection failed (non-fatal)")

    # Detect unexpectedly offline agents
    try:
        offline_agents = await check_for_unexpected_offline(config, active_sessions)
        escalation_target = config.escalation.escalation_target
        escalation_session = config.entities.sessions.get(escalation_target)

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
                    await send_message(escalation_session, msg)
                    log.warning("Escalated offline: %s", agent["entity"])
    except Exception:
        log.exception("Offline detection failed (non-fatal)")

    # Detect plan-waiting agents and send Telegram notification
    try:
        notification_chat_id = config.telegram.notification_chat_id
        telegram_token = os.environ.get("TELEGRAM_TOKEN", "")

        if notification_chat_id and telegram_token:
            now = time.monotonic()
            plan_dedup_seconds = 1800  # 30 minutes

            # Clean expired entries
            expired = [k for k, t in _plan_notify_dedup.items() if now - t > plan_dedup_seconds]
            for k in expired:
                del _plan_notify_dedup[k]

            state_path = config.agent_state.state_path
            stale_threshold = config.agent_state.stale_threshold_seconds

            for entity in config.entities.all_entities:
                session_name = config.entities.sessions.get(entity)
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
                sent = await BackboneBot.send_notification(
                    telegram_token, notification_chat_id, msg
                )
                if sent:
                    _plan_notify_dedup[dedup_key] = now
                    log.info("Sent plan-waiting notification for %s", entity)
    except Exception:
        log.exception("Plan-waiting notification failed (non-fatal)")

    # State-aware delivery loop
    for entity in config.entities.all_entities:
        session_name = config.entities.sessions.get(entity)
        if not session_name or session_name not in active_sessions:
            continue

        # Check agent state before delivery
        snapshot = await get_agent_state(
            config.agent_state.state_path,
            session_name,
            config.agent_state.stale_threshold_seconds,
        )

        # Monitor requires confirmed idle — only deliver to agents that are
        # definitively ready for work. This prevents startup floods and
        # respects agent processing cycles. Check early to avoid unnecessary API calls.
        if not should_deliver(snapshot.state, require_idle=True):
            result[entity] = "deferred"
            log.info("Deferred delivery to %s (state=%s)", entity, snapshot.state.value)
            continue

        # Get all pending issues for this entity
        pending_issues = await check_pending_issues(config, entity)
        if not pending_issues:
            result[entity] = "no_pending"
            continue

        # Iterate through pending issues — skip acknowledged and recently delivered,
        # deliver the first one that's ready
        delivered = False
        for candidate in pending_issues:
            # Check if already acknowledged (agent commented on this issue)
            is_acked = False
            try:
                async with BackboneDB(str(config.delivery.db_file)) as db:
                    if await db.is_acknowledged(candidate.number, entity):
                        is_acked = True
                    elif has_commented_on_issue(candidate.number, session_name):
                        await db.record_acknowledgment(candidate.number, entity)
                        is_acked = True
            except Exception:
                log.exception(
                    "Failed to check acknowledgment for #%d (non-fatal)",
                    candidate.number,
                )

            if is_acked:
                log.debug("Skipping #%d for %s — acknowledged", candidate.number, entity)
                continue

            # Skip if recently delivered by another flow
            recently_delivered = False
            try:
                async with BackboneDB(str(config.delivery.db_file)) as db:
                    recent = await db.query_deliveries(
                        issue_number=candidate.number,
                        target_entity=entity,
                        outcome="delivered",
                        limit=1,
                    )
                    if recent:
                        delivered_at = datetime.fromisoformat(recent[0]["created_at"])
                        now = datetime.now(UTC)
                        if delivered_at.tzinfo is None:
                            delivered_at = delivered_at.replace(tzinfo=UTC)
                        age = (now - delivered_at).total_seconds()
                        if age < config.scheduling.monitor_interval_seconds * 2:
                            recently_delivered = True
                            log.info(
                                "Skipping #%d for %s — delivered %ds ago by %s",
                                candidate.number,
                                entity,
                                int(age),
                                recent[0].get("flow_name", "?"),
                            )
            except Exception:
                log.exception(
                    "Failed to check recent deliveries for #%d (non-fatal)",
                    candidate.number,
                )

            if recently_delivered:
                continue

            # Found an unacknowledged, undelivered issue — deliver it
            message = format_next_issue_notification(candidate)
            if await send_message(session_name, message):
                result[entity] = f"delivered_#{candidate.number}"
                log.info("Delivered pending issue #%d to %s", candidate.number, entity)
                try:
                    async with BackboneDB(str(config.delivery.db_file)) as db:
                        await db.record_delivery(
                            issue_number=candidate.number,
                            target_entity=entity,
                            session_name=session_name,
                            outcome="delivered",
                            flow_name="agent-monitor",
                        )
                except Exception:
                    log.exception("Failed to record delivery (non-fatal)")
            else:
                result[entity] = "delivery_failed"
            delivered = True
            break

        if not delivered and not result.get(entity):
            result[entity] = "no_deliverable"

    return result


async def _sync_dependencies(config: BackboneConfig) -> None:
    """Sync sub-issue relationships to SQLite for close-time lookups."""
    async with BackboneDB(str(config.delivery.db_file)) as db:
        async with GitHubClient(config) as gh:
            # Check each entity's open issues for sub-issue relationships
            checked: set[int] = set()
            for entity in config.entities.all_entities:
                label = f"for:{entity}"
                issues = await gh.list_open_issues(label)
                for issue in issues:
                    if issue.number in checked:
                        continue
                    checked.add(issue.number)
                    subs = await gh.get_sub_issues(issue.number)
                    if subs:
                        await db.sync_dependencies(issue.number, [s.number for s in subs])
