"""Delivery retry: re-attempt failed issue deliveries and drain the queue."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_backbone.models import (
    BLOCKED_OUTCOMES,
    RETIREMENT_REASONS,
    DeliveryOutcome,
    EventType,
    IssueData,
)
from agent_backbone.services.jobs.diagnostics import observe_job
from agent_backbone.services.routing import (
    current_notification_issue,
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
    except Exception as exc:
        log.exception("Failed to recover stale leases (non-fatal)")
        await observe_job(db, source=SOURCE, stage="lease_recovery", error_type=type(exc).__name__)
    else:
        await observe_job(db, source=SOURCE, stage="lease_recovery")

    try:
        active_swarms = {row["name"] for row in await db.swarms.list(active_only=True)}
        protected = tuple(spec.name for spec in config.agents if spec.swarm in active_swarms)
        expired = await db.queue.expire_pending(
            max_age_minutes=config.timing.queue_expiry_minutes,
            protected_sessions=protected,
        )
        if expired:
            log.info(
                "Expired %d queued messages (> %d min)",
                len(expired),
                config.timing.queue_expiry_minutes,
            )
            summary["queue_expired"] = len(expired)  # each left a delivery row (same transaction)
    except Exception as exc:
        log.exception("Failed to expire stale messages (non-fatal)")
        await observe_job(db, source=SOURCE, stage="queue_expiry", error_type=type(exc).__name__)
    else:
        await observe_job(db, source=SOURCE, stage="queue_expiry")

    try:
        queued_sessions = set(await db.queue.sessions_with_pending())
    except Exception as exc:
        await observe_job(db, source=SOURCE, stage="queue_sessions", error_type=type(exc).__name__)
        raise
    else:
        await observe_job(db, source=SOURCE, stage="queue_sessions")
    for session_name in sorted(set(active_sessions) | queued_sessions):
        if session_name in _draining:
            continue
        _draining.add(session_name)
        try:
            completed = await _drain_session(config, db, gh, session_name, summary)
        except Exception as exc:
            log.exception("Queue drain failed for %s (other sessions continue)", session_name)
            await observe_job(
                db,
                source=SOURCE,
                stage="queue_drain",
                agent_name=session_name,
                error_type=type(exc).__name__,
            )
        else:
            if completed:
                await observe_job(db, source=SOURCE, stage="queue_drain", agent_name=session_name)
        finally:
            _draining.discard(session_name)
    return summary


async def _drain_session(config, db, gh, session_name, summary) -> bool:
    queued = await db.queue.dequeue(session_name, limit=5)
    completed = True
    try:
        for record in queued:
            target = record.get("target_entity")
            scope: set[tuple[str, int]] | None = None
            source_key = (record.get("dedup_key") or "").removeprefix("src:")
            if (
                record.get("delivery_kind") in {"comment", "review", "pull_request", "watch"}
                and record.get("repo")
                and record.get("issue_number")
            ):
                try:
                    _, retired = await current_notification_issue(
                        gh, record["repo"], record["issue_number"], source_key=source_key
                    )
                except Exception as exc:
                    await observe_job(
                        db,
                        source=SOURCE,
                        stage="notification_validity",
                        agent_name=session_name,
                        repo=record["repo"],
                        issue_number=record["issue_number"],
                        error_type=type(exc).__name__,
                    )
                    summary["queue_deferred"] = summary.get("queue_deferred", 0) + 1
                    completed = False
                    continue
                await observe_job(
                    db,
                    source=SOURCE,
                    stage="notification_validity",
                    agent_name=session_name,
                    repo=record["repo"],
                    issue_number=record["issue_number"],
                )
                if retired:
                    await db.queue.mark_delivered(record["id"], reason=retired)
                    summary["queue_cleared"] = summary.get("queue_cleared", 0) + 1
                    continue
            if record.get("delivery_kind") == "issue":
                try:
                    issue, status = await _current_issue(config, record, db, gh)
                    if status in RETIREMENT_REASONS:
                        await db.queue.mark_delivered(record["id"], reason=status)
                        summary["queue_cleared"] = summary.get("queue_cleared", 0) + 1
                        continue
                    if issue is None:
                        raise RuntimeError("Current issue could not be verified")
                    scope = queue_scope(await list_open_queue_for_target(config, target, gh, db=db))
                except Exception as exc:
                    # Without the open queue the acknowledgement gate would
                    # widen to every historical delivery (closed issues
                    # included) and could stall the whole queue. Defer: the
                    # rows go back to pending and the next drain retries.
                    log.exception("Failed to load queue scope for %s; deferring", target)
                    await observe_job(
                        db,
                        source=SOURCE,
                        stage="queue_scope",
                        agent_name=session_name,
                        repo=record.get("repo") or "",
                        issue_number=record.get("issue_number"),
                        error_type=type(exc).__name__,
                    )
                    index = queued.index(record)
                    summary["queue_deferred"] = summary.get("queue_deferred", 0) + (
                        len(queued) - index
                    )
                    completed = False
                    break
                else:
                    await observe_job(
                        db,
                        source=SOURCE,
                        stage="queue_scope",
                        agent_name=session_name,
                        repo=record.get("repo") or "",
                        issue_number=record.get("issue_number"),
                    )
            outcome = (
                await safe_deliver(
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
                    source_key=(
                        record["dedup_key"].removeprefix("src:")
                        if (record.get("dedup_key") or "").startswith("src:")
                        else None
                    ),
                    # The leased row already holds this message, including on failure.
                    requeue=False,
                    operation_id=record.get("operation_id"),
                    queue_id=record["id"],
                )
            ).outcome
            if outcome in _QUEUE_DONE:
                if outcome == DeliveryOutcome.ALREADY_DELIVERED:
                    await db.queue.mark_delivered(record["id"], reason="already_delivered")
                else:
                    await db.queue.mark_delivered(record["id"])
                key = "queue_delivered" if outcome == DeliveryOutcome.DELIVERED else "queue_cleared"
                summary[key] = summary.get(key, 0) + 1
            else:
                # Still blocked: release every remaining lease of this batch so
                # the next drain retries in a minute, not after the 5-minute
                # stale-lease sweep. Stop here to preserve oldest-first order.
                completed = False
                break
    finally:
        # Successful/retired rows ignore release; blocked, failed and cancelled
        # attempts put every remaining lease back for the next drain.
        for record in queued:
            await db.queue.release(record["id"])
    return completed


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
        issue, retired = await current_notification_issue(gh, repo, issue_number)
    except Exception as exc:
        log.warning("Failed to fetch %s#%d for retry", repo, issue_number)
        await observe_job(
            db,
            source=SOURCE,
            stage="issue_fetch",
            agent_name=session_name,
            repo=repo,
            issue_number=issue_number,
            error_type=type(exc).__name__,
        )
        return None, "fetch_failed"
    else:
        await observe_job(
            db,
            source=SOURCE,
            stage="issue_fetch",
            agent_name=session_name,
            repo=repo,
            issue_number=issue_number,
        )
    if retired:
        return None, retired
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

    scope = queue_scope(await list_open_queue_for_target(config, target, gh, db=db))
    operation_id = delivery.get("operation_id")
    if not operation_id and delivery.get("id") is not None:
        operation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"backbone:delivery:{delivery['id']}").hex
    outcome = (
        await safe_deliver(
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
            operation_id=operation_id,
        )
    ).outcome
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
    except Exception as exc:
        log.exception("Failed to reclaim stale attempts (non-fatal)")
        await observe_job(db, source=SOURCE, stage="claim_reclaim", error_type=type(exc).__name__)
    else:
        await observe_job(db, source=SOURCE, stage="claim_reclaim")

    try:
        summary.update(await retry_outbox(config, db, gh))
    except Exception as exc:
        log.exception("Could not load pending outbox events (other retries continue)")
        await observe_job(db, source=SOURCE, stage="outbox_load", error_type=type(exc).__name__)
    else:
        await observe_job(db, source=SOURCE, stage="outbox_load")

    if gh is not None:
        try:
            failures = await db.deliveries.failed(limit=20)
        except Exception as exc:
            log.exception("Could not load failed issues (queue drain continues)")
            await observe_job(
                db, source=SOURCE, stage="issue_retry_read", error_type=type(exc).__name__
            )
            failures = []
        else:
            await observe_job(db, source=SOURCE, stage="issue_retry_read")
        for delivery in failures:
            try:
                outcome = await retry_delivery(config, delivery, db, gh)
                if outcome in RETIREMENT_REASONS:
                    await db.deliveries.retire(delivery["id"], outcome)
                summary[outcome] = summary.get(outcome, 0) + 1
            except Exception as exc:
                log.exception("Could not retry delivery %s (continuing)", delivery["id"])
                summary["errors"] = summary.get("errors", 0) + 1
                await observe_job(
                    db,
                    source=SOURCE,
                    stage="issue_retry",
                    agent_name=delivery["session_name"],
                    repo=delivery.get("repo") or "",
                    issue_number=delivery.get("issue_number"),
                    error_type=type(exc).__name__,
                )
            else:
                if outcome != "fetch_failed":
                    await observe_job(
                        db,
                        source=SOURCE,
                        stage="issue_retry",
                        agent_name=delivery["session_name"],
                        repo=delivery.get("repo") or "",
                        issue_number=delivery.get("issue_number"),
                    )

    try:
        drained = await drain_message_queue(
            config, db, gh, active_sessions=set(await list_sessions())
        )
        for key, value in drained.items():
            summary[key] = summary.get(key, 0) + value
    except Exception as exc:
        log.exception("Queue drain failed (non-fatal)")
        await observe_job(db, source=SOURCE, stage="queue_dispatch", error_type=type(exc).__name__)
    else:
        # This scope covers loading/dispatching the session list. Individual
        # session failures are observed independently above, never cleared here.
        await observe_job(db, source=SOURCE, stage="queue_dispatch")

    if summary:
        log.info("Retry complete: %s", summary)
    return summary
