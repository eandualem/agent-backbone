"""Delivery retry: re-attempt failed issue deliveries and drain the queue."""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_backbone.models import BLOCKED_OUTCOMES, DeliveryOutcome, EventType, IssueData
from agent_backbone.services.routing import (
    format_next_issue_notification,
    is_acknowledged,
    list_open_queue_for_target,
    queue_scope,
    retry_outbox,
    route_issue,
    safe_deliver,
    stamp_queued_age,
)
from agent_backbone.services.terminal import list_sessions

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)

_BUSY_OUTCOMES = BLOCKED_OUTCOMES - {DeliveryOutcome.OFFLINE}
_QUEUE_DONE = frozenset({DeliveryOutcome.DELIVERED, DeliveryOutcome.ALREADY_DELIVERED})
_RETIRED = frozenset({"acknowledged", "no_repo", "issue_closed", "no_longer_targeted"})
SOURCE = "delivery-retry"
_draining: set[str] = set()
"""Sessions currently draining; removed on completion or cancellation."""


def _waited_seconds(record: dict) -> float:
    """How long a queued row has been waiting (its ``enqueued_at`` is ISO 8601)."""
    raw = record.get("enqueued_at") or ""
    try:
        enqueued = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if enqueued.tzinfo is None:
        enqueued = enqueued.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - enqueued).total_seconds())


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
                len(expired),
                config.timing.queue_expiry_minutes,
            )
            summary["queue_expired"] = len(expired)  # each left a delivery row (same transaction)
    except Exception:
        log.exception("Failed to expire stale messages (non-fatal)")

    queued_sessions = set(await db.queue.sessions_with_pending())
    for session_name in sorted(set(active_sessions) | queued_sessions):
        if session_name in _draining:
            continue
        _draining.add(session_name)
        try:
            await _drain_session(config, db, gh, session_name, summary)
        except Exception:
            log.exception("Queue drain failed for %s (other sessions continue)", session_name)
        finally:
            _draining.discard(session_name)
    return summary


async def _drain_session(config, db, gh, session_name, summary) -> None:
    queued = await db.queue.dequeue(session_name, limit=5)
    try:
        for record in queued:
            target = record.get("target_entity")
            scope: set[tuple[str, int]] | None = None
            if record.get("delivery_kind") == "issue":
                try:
                    issue, status = await _current_issue(config, record, db, gh)
                    if status in _RETIRED:
                        await db.queue.mark_delivered(record["id"])
                        summary["queue_cleared"] = summary.get("queue_cleared", 0) + 1
                        continue
                    if issue is None:
                        raise RuntimeError("Current issue could not be verified")
                    scope = queue_scope(await list_open_queue_for_target(config, target, gh))
                except Exception:
                    # Without the open queue the acknowledgement gate would
                    # widen to every historical delivery (closed issues
                    # included) and could stall the whole queue. Defer: the
                    # rows go back to pending and the next drain retries.
                    log.exception("Failed to load queue scope for %s; deferring", target)
                    index = queued.index(record)
                    summary["queue_deferred"] = summary.get("queue_deferred", 0) + (
                        len(queued) - index
                    )
                    break
            outcome = await safe_deliver(
                session_name,
                stamp_queued_age(record["message"], _waited_seconds(record)),
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
                # The leased row already holds this message, including on failure.
                requeue=False,
            )
            if outcome in _QUEUE_DONE:
                await db.queue.mark_delivered(record["id"])
                key = "queue_delivered" if outcome == DeliveryOutcome.DELIVERED else "queue_cleared"
                summary[key] = summary.get(key, 0) + 1
            else:
                # Still blocked: release every remaining lease of this batch so
                # the next drain retries in a minute, not after the 5-minute
                # stale-lease sweep. Stop here to preserve oldest-first order.
                break
    finally:
        # Successful/retired rows ignore release; blocked, failed and cancelled
        # attempts put every remaining lease back for the next drain.
        for record in queued:
            await db.queue.release(record["id"])


async def _current_issue(
    config: BackboneConfig, delivery: dict, db: BackboneDB, gh: GitHubClient | None
) -> tuple[IssueData | None, str | None]:
    """Validate stored work against today's routing, before retry or queue drain."""
    session_name = delivery["session_name"]
    issue_number = delivery["issue_number"]
    target = delivery["target_entity"]
    repo = delivery.get("repo") or ""

    if await is_acknowledged(db, repo, issue_number, target, session_name):
        return None, "acknowledged"
    if not repo:
        return None, "no_repo"

    if gh is None:
        return None, "fetch_failed"

    try:
        issue = await gh.get_issue(issue_number, repo_full_name=repo)
    except Exception:
        log.warning("Failed to fetch %s#%d for retry", repo, issue_number)
        return None, "fetch_failed"
    if issue.state == "closed":
        return None, "issue_closed"
    if (
        issue.labels.sender == target
        or target not in route_issue(issue, EventType.ISSUE_OPENED, config).queue
    ):
        return None, "no_longer_targeted"
    return issue, None


async def retry_delivery(
    config: BackboneConfig, delivery: dict, db: BackboneDB, gh: GitHubClient
) -> str:
    """Re-attempt one failed issue delivery after checking its current audience."""
    issue, status = await _current_issue(config, delivery, db, gh)
    if status:
        return status
    session_name = delivery["session_name"]
    issue_number = delivery["issue_number"]
    target = delivery["target_entity"]
    repo = delivery.get("repo") or ""

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

    try:
        summary.update(await retry_outbox(config, db, gh))
    except Exception:
        log.exception("Could not load pending outbox events (other retries continue)")

    if gh is not None:
        try:
            failures = await db.deliveries.failed(limit=20)
        except Exception:
            log.exception("Could not load failed issues (queue drain continues)")
            failures = []
        for delivery in failures:
            try:
                outcome = await retry_delivery(config, delivery, db, gh)
                if outcome in _RETIRED:
                    await db.deliveries.retire(delivery["id"], outcome)
                summary[outcome] = summary.get(outcome, 0) + 1
            except Exception:
                log.exception("Could not retry delivery %s (continuing)", delivery["id"])
                summary["errors"] = summary.get("errors", 0) + 1

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
