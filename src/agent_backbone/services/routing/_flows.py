"""Delivery retry: re-attempt failed issue deliveries and drain the queue."""

from __future__ import annotations

import logging
from collections.abc import Collection

from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import format_next_issue_notification
from agent_backbone.services.routing._targets import list_open_queue_for_target, queue_scope

log = logging.getLogger(__name__)

_BUSY_OUTCOMES = frozenset({"agent_working", "waiting_for_human", "human_typing", "settling"})


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
            summary["leases_recovered"] = stale_leases
    except Exception:
        log.exception("Failed to recover stale leases (non-fatal)")

    try:
        expired = await db.expire_stale_pending(
            max_age_minutes=config.delivery.queue_expiry_minutes
        )
        if expired:
            log.info(
                "Expired %d queued messages (> %d min)",
                expired,
                config.delivery.queue_expiry_minutes,
            )
            summary["queue_expired"] = expired
    except Exception:
        log.exception("Failed to expire stale messages (non-fatal)")

    queued_sessions = set(await db.get_sessions_with_pending())
    for session_name in sorted(set(active_sessions) | queued_sessions):
        queued = await db.dequeue_messages(session_name, limit=5)
        if not queued:
            continue
        for record in queued:
            target = record.get("target_entity")
            scope: set[tuple[str, int]] | None = None
            if target and gh is not None and record.get("delivery_kind") == "issue":
                try:
                    scope = queue_scope(await list_open_queue_for_target(config, target, gh))
                except Exception:
                    log.exception("Failed to load queue scope for %s (non-fatal)", target)
            outcome = await safe_deliver(
                session_name,
                record["message"],
                config,
                db=db,
                repo=record.get("repo") or "",
                issue_number=record.get("issue_number"),
                target_entity=target,
                flow_name="delivery-retry-queue",
                enforce_issue_queue=True,
                queue_scope=scope,
                delivery_kind=record.get("delivery_kind", "issue"),
            )
            if outcome in ("delivered", "already_delivered"):
                await db.mark_message_delivered(record["id"])
                key = "queue_delivered" if outcome == "delivered" else "queue_cleared"
                summary[key] = summary.get(key, 0) + 1
            else:
                await db.release_lease(record["id"])
                break
    return summary


async def retry_delivery(config: BackboneConfig, delivery: dict, db: BackboneDB, gh: object) -> str:
    """Re-attempt one failed issue delivery."""
    session_name = delivery["session_name"]
    issue_number = delivery["issue_number"]
    target = delivery["target_entity"]
    repo = delivery.get("repo") or ""

    if await db.is_acknowledged(issue_number, target, repo=repo):
        return "acknowledged"
    if session_name != target and await db.is_acknowledged(issue_number, session_name, repo=repo):
        return "acknowledged"
    if not repo:
        return "no_repo"

    try:
        issue = await gh.get_issue(issue_number, repo_full_name=repo)
    except Exception:
        log.warning("Failed to fetch %s#%d for retry", repo, issue_number)
        return "fetch_failed"
    if issue.state == "closed":
        return "issue_closed"

    scope = queue_scope(await list_open_queue_for_target(config, target, gh))
    outcome = await safe_deliver(
        session_name,
        format_next_issue_notification(issue),
        config,
        db=db,
        repo=repo,
        issue_number=issue_number,
        target_entity=target,
        flow_name="delivery-retry",
        enforce_issue_queue=True,
        queue_scope=scope,
    )
    if outcome == "delivered":
        return "retried"
    if outcome == "offline":
        return "still_offline"
    if outcome in _BUSY_OUTCOMES:
        return "still_busy"
    if outcome in ("already_delivered", "awaiting_ack"):
        return outcome
    return "delivery_failed"


async def delivery_retry(config: BackboneConfig, db: BackboneDB, gh: object | None) -> dict:
    """Retry failed issue deliveries, then drain the queue."""
    summary: dict[str, int] = {}
    try:
        reclaimed = await db.reclaim_stale_attempts(max_age_minutes=5)
        if reclaimed:
            summary["attempts_reclaimed"] = reclaimed
    except Exception:
        log.exception("Failed to reclaim stale attempts (non-fatal)")

    if gh is not None:
        for delivery in await db.get_failed_deliveries(limit=20):
            outcome = await retry_delivery(config, delivery, db, gh)
            summary[outcome] = summary.get(outcome, 0) + 1

    try:
        from agent_backbone.services.terminal import list_sessions

        drained = await drain_message_queue(
            config, db, gh, active_sessions=set(await list_sessions())
        )
        for key, value in drained.items():
            summary[key] = summary.get(key, 0) + value
    except Exception:
        log.exception("Queue drain failed (non-fatal)")

    if summary:
        log.info("Retry complete: %s", summary)
    return summary
