"""Delivery flows: retry failed deliveries and scheduled delivery.

Runs on configurable intervals. Queries the persistence layer for
failed/offline/deferred deliveries and retries them if the target
agent is now online and not busy.
"""

from __future__ import annotations

import logging

from prefect import flow, task

from agent_backbone.config import BackboneConfig
from agent_backbone.services._locator import ensure_initialized, get_config, get_db, get_gh
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import format_next_issue_notification

log = logging.getLogger(__name__)


@task
async def retry_delivery(config: BackboneConfig, delivery: dict, db: BackboneDB, gh: object) -> str:
    """Attempt to retry a single failed delivery.

    Returns outcome string: retried, still_offline, still_busy, issue_closed.
    """
    session_name = delivery["session_name"]
    issue_number = delivery["issue_number"]
    target_entity = delivery["target_entity"]

    # Skip if already acknowledged (check both entity and session for fallback scenarios)
    if await db.is_acknowledged(issue_number, target_entity):
        return "acknowledged"
    if session_name != target_entity and await db.is_acknowledged(issue_number, session_name):
        return "acknowledged"

    # Fetch current issue state from GitHub
    try:
        issue = await gh.get_issue(issue_number)
    except Exception:
        log.warning("Failed to fetch issue #%d for retry", issue_number)
        return "fetch_failed"

    # Skip if issue is now closed
    if issue.state == "closed":
        return "issue_closed"

    # Deliver via safe_deliver (handles state checks + enqueue on failure)
    message = format_next_issue_notification(issue)
    queue_scope_issue_numbers = {
        item.number
        for item in await gh.list_open_issues(
            f"for:{target_entity}",
            repo_full_name=issue.repo_full_name or None,
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
        log.info(
            "Retry delivered #%d to %s (%s)",
            issue_number,
            target_entity,
            session_name,
        )
        return "retried"
    if outcome == "offline":
        return "still_offline"
    busy_states = ("agent_working", "plan_waiting", "copy_mode", "user_interacting", "grace_period")
    if outcome in busy_states:
        return "still_busy"
    if outcome in ("already_delivered", "awaiting_ack", "unknown_state"):
        return outcome
    return "delivery_failed"


@flow(name="delivery-retry")
async def delivery_retry() -> dict:
    """Retry failed deliveries for agents that are now online.

    Returns summary of retry outcomes.
    """
    await ensure_initialized()

    config = get_config()
    db = get_db()
    gh = get_gh()
    summary: dict[str, int] = {}

    failed = await db.get_failed_deliveries(limit=20)

    if not failed:
        log.info("No failed deliveries to retry")
        return summary

    log.info("Found %d failed deliveries to retry", len(failed))

    for delivery in failed:
        outcome = await retry_delivery(config, delivery, db, gh)
        summary[outcome] = summary.get(outcome, 0) + 1

    # Drain message queue: deliver pending queued messages
    try:
        from agent_backbone.services.terminal import list_sessions as _list_sessions

        active_sessions = set(await _list_sessions())
        for session_name in active_sessions:
            queued = await db.dequeue_messages(session_name, limit=5)
            for msg_record in queued:
                q_outcome = await safe_deliver(
                    session_name,
                    msg_record["message"],
                    config,
                    db=db,
                    issue_number=msg_record.get("issue_number"),
                    target_entity=msg_record.get("target_entity"),
                    flow_name="delivery-retry-queue",
                    enforce_issue_queue=True,
                    queue_scope_issue_numbers={
                        item.number
                        for item in await gh.list_open_issues(f"for:{msg_record['target_entity']}")
                    }
                    if msg_record.get("target_entity")
                    else None,
                )
                if q_outcome == "delivered":
                    await db.mark_message_delivered(msg_record["id"])
                    summary["queue_delivered"] = summary.get("queue_delivered", 0) + 1
                else:
                    break  # Stop draining this session if delivery fails
    except Exception:
        log.exception("Queue drain failed (non-fatal)")

    log.info("Retry complete: %s", summary)
    return summary


@flow(name="scheduled-delivery")
async def scheduled_delivery(
    issue_number: int,
    target_entity: str,
    session_name: str,
    is_blocking: bool = False,
) -> str:
    """Deliver a specific issue to a specific session.

    Returns outcome string: delivered, offline, busy, issue_closed.
    """
    await ensure_initialized()

    config = get_config()
    db = get_db()
    gh = get_gh()

    # Fetch current issue state
    issue = await gh.get_issue(issue_number)

    if issue.state == "closed":
        return "issue_closed"

    # Deliver via safe_deliver (handles state checks)
    message = format_next_issue_notification(issue)
    queue_scope_issue_numbers = {
        item.number
        for item in await gh.list_open_issues(
            f"for:{target_entity}",
            repo_full_name=issue.repo_full_name or None,
        )
    }
    outcome = await safe_deliver(
        session_name,
        message,
        config,
        db=db,
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
    busy_states = ("agent_working", "plan_waiting", "copy_mode", "user_interacting", "grace_period")
    if outcome in busy_states:
        return "busy"
    if outcome in ("already_delivered", "awaiting_ack", "unknown_state"):
        return outcome
    return "delivery_failed"
