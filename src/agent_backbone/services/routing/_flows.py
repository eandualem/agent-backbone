"""Delivery retry: re-attempt failed deliveries and drain the message queue.

Run periodically by the in-process scheduler. Queries the persistence layer
for failed/offline/deferred deliveries and retries them if the target agent
is now online and idle.
"""

from __future__ import annotations

import logging
from collections.abc import Collection

from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import format_next_issue_notification
from agent_backbone.services.routing._targets import (
    list_open_queue_for_target,
    repo_full_name_for_target,
)

log = logging.getLogger(__name__)

_BUSY_OUTCOMES = frozenset(
    {
        "agent_working",
        "plan_waiting",
        "permission_waiting",
        "user_interacting",
        "grace_period",
    }
)


async def drain_message_queue(
    config: BackboneConfig,
    db: BackboneDB,
    gh: object | None,
    *,
    active_sessions: Collection[str],
) -> dict[str, int]:
    """Drain queued messages for active sessions, oldest first."""
    summary: dict[str, int] = {}

    try:
        stale_leases = await db.expire_stale_leases(max_age_minutes=5)
        if stale_leases:
            log.info("Recovered %d stale leased messages", stale_leases)
            summary["leases_recovered"] = stale_leases
    except Exception:
        log.exception("Failed to recover stale leases (non-fatal)")

    try:
        expired = await db.expire_stale_pending(max_age_minutes=30)
        if expired:
            log.info("Expired %d stale queued messages (>30min old)", expired)
            summary["queue_expired"] = expired
    except Exception:
        log.exception("Failed to expire stale messages (non-fatal)")

    queued_sessions = set(await db.get_sessions_with_pending())
    all_sessions = set(active_sessions) | queued_sessions

    for session_name in sorted(all_sessions):
        queued = await db.dequeue_messages(session_name, limit=5)
        if not isinstance(queued, list) or not queued:
            continue

        for msg_record in queued:
            target_entity = msg_record.get("target_entity")
            queue_scope: set[int] | None = None
            if target_entity and gh is not None:
                try:
                    queue_scope = {
                        item.number
                        for item in await list_open_queue_for_target(config, target_entity, gh)
                    }
                except Exception:
                    log.exception("Failed to load queue scope for %s (non-fatal)", target_entity)

            q_outcome = await safe_deliver(
                session_name,
                msg_record["message"],
                config,
                db=db,
                issue_number=msg_record.get("issue_number"),
                target_entity=target_entity,
                flow_name="delivery-retry-queue",
                enforce_issue_queue=True,
                queue_scope_issue_numbers=queue_scope,
                delivery_kind=msg_record.get("delivery_kind", "issue"),
            )
            if q_outcome == "delivered":
                await db.mark_message_delivered(msg_record["id"])
                summary["queue_delivered"] = summary.get("queue_delivered", 0) + 1
            elif q_outcome == "already_delivered":
                await db.mark_message_delivered(msg_record["id"])
                summary["queue_cleared"] = summary.get("queue_cleared", 0) + 1
            else:
                await db.release_lease(msg_record["id"])
                break  # Stop draining this session if delivery fails

    return summary


async def retry_delivery(config: BackboneConfig, delivery: dict, db: BackboneDB, gh: object) -> str:
    """Attempt to retry a single failed delivery.

    Returns outcome string: retried, still_offline, still_busy, issue_closed, ...
    """
    session_name = delivery["session_name"]
    issue_number = delivery["issue_number"]
    target_entity = delivery["target_entity"]

    if await db.is_acknowledged(issue_number, target_entity):
        return "acknowledged"
    if session_name != target_entity and await db.is_acknowledged(issue_number, session_name):
        return "acknowledged"

    repo_full_name = repo_full_name_for_target(target_entity, config)
    try:
        issue = await gh.get_issue(issue_number, repo_full_name=repo_full_name)
    except Exception:
        log.warning("Failed to fetch issue #%d for retry", issue_number)
        return "fetch_failed"

    if issue.state == "closed":
        return "issue_closed"

    message = format_next_issue_notification(issue)
    queue_scope_issue_numbers = {
        item.number
        for item in await list_open_queue_for_target(
            config,
            target_entity,
            gh,
            issue_repo_full_name=issue.repo_full_name,
        )
    }
    outcome = await safe_deliver(
        session_name,
        message,
        config,
        db=db,
        issue_number=issue_number,
        target_entity=target_entity,
        flow_name="delivery-retry",
        enforce_issue_queue=True,
        queue_scope_issue_numbers=queue_scope_issue_numbers,
    )

    if outcome == "delivered":
        log.info("Retry delivered #%d to %s (%s)", issue_number, target_entity, session_name)
        return "retried"
    if outcome == "offline":
        return "still_offline"
    if outcome in _BUSY_OUTCOMES:
        return "still_busy"
    if outcome in ("already_delivered", "awaiting_ack", "unknown_state"):
        return outcome
    return "delivery_failed"


async def delivery_retry(config: BackboneConfig, db: BackboneDB, gh: object | None) -> dict:
    """Retry failed deliveries for agents that are now online, then drain the queue."""
    summary: dict[str, int] = {}

    try:
        reclaimed = await db.reclaim_stale_attempts(max_age_minutes=5)
        if reclaimed:
            log.info("Reclaimed %d stale delivery attempts", reclaimed)
            summary["attempts_reclaimed"] = reclaimed
    except Exception:
        log.exception("Failed to reclaim stale attempts (non-fatal)")

    if gh is not None:
        failed = await db.get_failed_deliveries(limit=20)
        if failed:
            log.info("Found %d failed deliveries to retry", len(failed))
            for delivery in failed:
                outcome = await retry_delivery(config, delivery, db, gh)
                summary[outcome] = summary.get(outcome, 0) + 1

    try:
        from agent_backbone.services.terminal import list_sessions

        active_sessions = set(await list_sessions())
        drained = await drain_message_queue(config, db, gh, active_sessions=active_sessions)
        for key, value in drained.items():
            summary[key] = summary.get(key, 0) + value
    except Exception:
        log.exception("Queue drain failed (non-fatal)")

    if summary:
        log.info("Retry complete: %s", summary)
    return summary
