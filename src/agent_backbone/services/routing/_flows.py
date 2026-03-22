"""Delivery flows: retry failed deliveries and scheduled delivery.

Runs on configurable intervals. Queries the persistence layer for
failed/offline/deferred deliveries and retries them if the target
agent is now online and not busy.
"""

from __future__ import annotations

import logging
from collections.abc import Collection

from agent_backbone.config import BackboneConfig
from agent_backbone.models import acknowledgment_entities
from agent_backbone.services._locator import get_config, get_db, get_gh
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import format_next_issue_notification
from agent_backbone.services.routing._targets import list_open_queue_for_target

log = logging.getLogger(__name__)


async def drain_message_queue(
    config: BackboneConfig,
    db: BackboneDB,
    gh: object,
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

    # Auto-expire stale pending messages to prevent infinite retry loops
    try:
        expired = await db.expire_stale_pending(max_age_minutes=30)
        if expired:
            log.info("Expired %d stale queued messages (>30min old)", expired)
            summary["queue_expired"] = expired
    except Exception:
        log.exception("Failed to expire stale messages (non-fatal)")

    queued_sessions = set(await db.get_sessions_with_pending())
    all_sessions = set(active_sessions) | queued_sessions

    for session_name in all_sessions:
        queued = await db.dequeue_messages(session_name, limit=5)
        if not isinstance(queued, list) or not queued:
            continue

        # Deduplicate by content_hash: deliver each unique message once
        seen_hashes: set[str] = set()
        for msg_record in queued:
            content_hash = msg_record.get("content_hash")
            if content_hash and content_hash in seen_hashes:
                await db.mark_message_delivered(msg_record["id"])
                summary["queue_deduped"] = (
                    summary.get("queue_deduped", 0) + 1
                )
                continue
            if content_hash:
                seen_hashes.add(content_hash)
            q_outcome = await safe_deliver(
                session_name,
                msg_record["message"],
                config,
                db=db,
                repo_full_name=msg_record.get("repo_full_name"),
                issue_number=msg_record.get("issue_number"),
                target_entity=msg_record.get("target_entity"),
                flow_name="delivery-retry-queue",
                enforce_issue_queue=True,
                queue_scope_issue_numbers={
                    item.identity_key
                    for item in await list_open_queue_for_target(
                        config,
                        msg_record["target_entity"],
                        gh,
                        issue_repo_full_name=msg_record.get("repo_full_name") or "",
                    )
                }
                if msg_record.get("target_entity")
                else None,
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


async def retry_delivery(
    config: BackboneConfig,
    delivery: dict,
    db: BackboneDB,
    gh: object,
    *,
    active_sessions: set[str] | None = None,
) -> str:
    """Attempt to retry a single failed delivery.

    Returns outcome string: retried, still_offline, still_busy, issue_closed.
    """
    target_entity = delivery["target_entity"]
    session_name = delivery["session_name"]
    issue_number = delivery["issue_number"]
    repo_full_name = delivery.get("repo_full_name") or (
        f"{config.github.owner}/{target_entity}"
        if target_entity in config.registry.repo_names
        else None
    )

    # Skip if session is not active — avoids creating new failure records
    if active_sessions is not None and session_name not in active_sessions:
        return "still_offline"

    # Skip if already acknowledged (check both entity and session for fallback scenarios)
    for entity in acknowledgment_entities(
        target_entity,
        repo_full_name,
        session_name=session_name,
    ):
        if await db.is_acknowledged(repo_full_name, issue_number, entity):
            return "acknowledged"

    # Fetch current issue state from GitHub
    try:
        issue = await gh.get_issue(
            issue_number,
            repo_full_name=repo_full_name,
        )
    except Exception:
        log.warning("Failed to fetch issue #%d for retry", issue_number)
        return "fetch_failed"

    # Skip if issue is now closed
    if issue.state == "closed":
        return "issue_closed"

    # Deliver via safe_deliver (handles state checks + enqueue on failure)
    message = format_next_issue_notification(issue)
    queue_scope_issue_numbers = {
        item.identity_key
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
        repo_full_name=issue.repo_full_name,
        issue_number=issue_number,
        target_entity=target_entity,
        flow_name="delivery-retry",
        enforce_issue_queue=True,
        queue_scope_issue_numbers=queue_scope_issue_numbers,
    )

    if outcome == "delivered":
        log.info(
            "Retry delivered #%d to %s (%s)",
            issue_number,
            target_entity,
            session_name,
        )
        return "retried"
    if outcome == "offline":
        return "still_offline"
    busy_states = (
        "agent_working",
        "plan_waiting",
        "permission_waiting",
        "user_interacting",
        "grace_period",
    )
    if outcome in busy_states:
        return "still_busy"
    if outcome in ("already_delivered", "awaiting_ack", "unknown_state"):
        return outcome
    return "delivery_failed"


async def delivery_retry() -> dict:
    """Retry failed deliveries for agents that are now online.

    Returns summary of retry outcomes.
    """
    config = get_config()
    db = get_db()
    gh = get_gh()
    summary: dict[str, int] = {}

    try:
        reclaimed = await db.reclaim_stale_attempts(max_age_minutes=5)
        if reclaimed:
            log.info("Reclaimed %d stale delivery attempts", reclaimed)
            summary["attempts_reclaimed"] = reclaimed
    except Exception:
        log.exception("Failed to reclaim stale attempts (non-fatal)")

    # Get active sessions early — used for both retry filtering and queue drain
    from agent_backbone.services.terminal import list_sessions as _list_sessions

    try:
        active_sessions = set(await _list_sessions())
    except Exception:
        log.exception("Failed to list sessions for retry (non-fatal)")
        active_sessions = set()

    failed = await db.get_failed_deliveries(limit=20)

    if failed:
        log.info("Found %d failed deliveries to retry", len(failed))
        for delivery in failed:
            outcome = await retry_delivery(
                config, delivery, db, gh, active_sessions=active_sessions
            )
            summary[outcome] = summary.get(outcome, 0) + 1
    else:
        log.info("No failed deliveries to retry; draining queued messages only")

    # Drain message queue: deliver pending queued messages
    try:
        drained = await drain_message_queue(config, db, gh, active_sessions=active_sessions)
        for key, value in drained.items():
            summary[key] = summary.get(key, 0) + value
    except Exception:
        log.exception("Queue drain failed (non-fatal)")

    log.info("Retry complete: %s", summary)
    return summary


async def scheduled_delivery(
    issue_number: int,
    target_entity: str,
    session_name: str,
    is_blocking: bool = False,
    repo_full_name: str = "",
) -> str:
    """Deliver a specific issue to a specific session.

    Returns outcome string: delivered, offline, busy, issue_closed.
    """
    config = get_config()
    db = get_db()
    gh = get_gh()

    # Fetch current issue state
    issue = await gh.get_issue(
        issue_number,
        repo_full_name=repo_full_name
        or (
            f"{config.github.owner}/{target_entity}"
            if target_entity in config.registry.repo_names
            else None
        ),
    )

    if issue.state == "closed":
        return "issue_closed"

    # Deliver via safe_deliver (handles state checks)
    message = format_next_issue_notification(issue)
    queue_scope_issue_numbers = {
        item.identity_key
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
        repo_full_name=issue.repo_full_name,
        issue_number=issue_number,
        target_entity=target_entity,
        flow_name="scheduled-delivery",
        priority=is_blocking,
        enforce_issue_queue=True,
        queue_scope_issue_numbers=queue_scope_issue_numbers,
    )

    if outcome == "delivered":
        log.info("Scheduled delivery of #%d to %s", issue_number, session_name)
        return "delivered"

    if outcome == "offline":
        return "offline"
    busy_states = (
        "agent_working",
        "plan_waiting",
        "permission_waiting",
        "user_interacting",
        "grace_period",
    )
    if outcome in busy_states:
        return "busy"
    if outcome in ("already_delivered", "awaiting_ack", "unknown_state"):
        return outcome
    return "delivery_failed"
