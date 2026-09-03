"""Delivery retry: re-attempt failed issue deliveries and drain the queue."""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import TYPE_CHECKING

from agent_backbone.models import BLOCKED_OUTCOMES, DeliveryOutcome
from agent_backbone.services.routing import (
    format_next_issue_notification,
    is_acknowledged,
    list_open_queue_for_target,
    queue_scope,
    safe_deliver,
)
from agent_backbone.services.terminal import list_sessions

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)

_BUSY_OUTCOMES = BLOCKED_OUTCOMES - {DeliveryOutcome.OFFLINE}
_QUEUE_DONE = frozenset({DeliveryOutcome.DELIVERED, DeliveryOutcome.ALREADY_DELIVERED})
SOURCE = "delivery-retry"


async def drain_message_queue(
    config: BackboneConfig,
    db: BackboneDB,
    gh: GitHubClient | None,
    *,
    active_sessions: Collection[str],
) -> dict[str, int]:
    """Drain queued messages for active sessions, oldest first."""
    summary: dict[str, int] = {}

    try:
        stale_leases = await db.queue.expire_stale_leases(max_age_minutes=5)
        if stale_leases:
            summary["leases_recovered"] = stale_leases
    except Exception:
        log.exception("Failed to recover stale leases (non-fatal)")

    try:
        expired = await db.queue.expire_pending(max_age_minutes=config.timing.queue_expiry_minutes)
        if expired:
            log.info(
                "Expired %d queued messages (> %d min)",
                expired,
                config.timing.queue_expiry_minutes,
            )
            summary["queue_expired"] = expired
    except Exception:
        log.exception("Failed to expire stale messages (non-fatal)")

    queued_sessions = set(await db.queue.sessions_with_pending())
    for session_name in sorted(set(active_sessions) | queued_sessions):
        queued = await db.queue.dequeue(session_name, limit=5)
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
                source=f"{SOURCE}-queue",
                enforce_issue_queue=True,
                queue_scope=scope,
                delivery_kind=record.get("delivery_kind", "issue"),
                sender=record.get("sender") or "",
            )
            if outcome in _QUEUE_DONE:
                await db.queue.mark_delivered(record["id"])
                key = "queue_delivered" if outcome == DeliveryOutcome.DELIVERED else "queue_cleared"
                summary[key] = summary.get(key, 0) + 1
            else:
                # Still blocked: release every remaining lease of this batch so
                # the next drain retries in a minute, not after the 5-minute
                # stale-lease sweep. Stop here to preserve oldest-first order.
                index = queued.index(record)
                for leased in queued[index:]:
                    await db.queue.release(leased["id"])
                break
    return summary


async def retry_delivery(
    config: BackboneConfig, delivery: dict, db: BackboneDB, gh: GitHubClient
) -> str:
    """Re-attempt one failed issue delivery."""
    session_name = delivery["session_name"]
    issue_number = delivery["issue_number"]
    target = delivery["target_entity"]
    repo = delivery.get("repo") or ""

    if await is_acknowledged(db, repo, issue_number, target, session_name):
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
        source=SOURCE,
        enforce_issue_queue=True,
        queue_scope=scope,
    )
    if outcome == DeliveryOutcome.DELIVERED:
        return "retried"
    if outcome == DeliveryOutcome.OFFLINE:
        return "still_offline"
    if outcome in _BUSY_OUTCOMES:
        return "still_busy"
    if outcome in (DeliveryOutcome.ALREADY_DELIVERED, DeliveryOutcome.AWAITING_ACK):
        return outcome.value
    return DeliveryOutcome.DELIVERY_FAILED.value


async def delivery_retry(config: BackboneConfig, db: BackboneDB, gh: GitHubClient | None) -> dict:
    """Retry failed issue deliveries, then drain the queue."""
    summary: dict[str, int] = {}
    try:
        reclaimed = await db.deliveries.reclaim_stale(max_age_minutes=5)
        if reclaimed:
            summary["attempts_reclaimed"] = reclaimed
    except Exception:
        log.exception("Failed to reclaim stale attempts (non-fatal)")

    if gh is not None:
        for delivery in await db.deliveries.failed(limit=20):
            outcome = await retry_delivery(config, delivery, db, gh)
            summary[outcome] = summary.get(outcome, 0) + 1

    try:
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
