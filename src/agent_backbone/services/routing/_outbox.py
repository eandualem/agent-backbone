"""Send persisted GitHub notifications, retrying only unresolved recipients."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from agent_backbone.models import DeliveryOutcome, EventType
from agent_backbone.services.routing._delivery import is_acknowledged, safe_deliver
from agent_backbone.services.routing._resolution import resolve_entity_session
from agent_backbone.services.routing._targets import (
    list_open_queue_for_target,
    queue_scope,
    route_issue,
)
from agent_backbone.services.routing._validity import current_notification_issue
from agent_backbone.services.routing.models import DispatchResult

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)
_locks: WeakValueDictionary[tuple[int, int, int], asyncio.Lock] = WeakValueDictionary()
_DONE = {"delivered", "queued", "skipped"}


async def flush_outbox(
    event_id: int,
    config: BackboneConfig,
    db: BackboneDB,
    gh: GitHubClient | None,
    *,
    plan: list[dict] | None = None,
) -> DispatchResult:
    """Persist a new plan, or resume its receipts under one event lock."""
    key = (id(asyncio.get_running_loop()), id(db), event_id)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        rows = await db.outbox.entries(event_id)
        retry = bool(rows)
        if not rows and plan is not None:
            await db.outbox.plan(event_id, plan)
            rows = await db.outbox.entries(event_id)
        result = DispatchResult()
        for row in rows:
            recipient = row["recipient"]
            if row["status"] in _DONE:
                result.skipped.append(recipient)
                continue
            delivery = dict(row["delivery"])
            # The durable recipient identity survives replay and process restarts.
            delivery["operation_id"] = uuid.uuid5(
                uuid.NAMESPACE_URL, f"backbone:outbox:{event_id}:{recipient}"
            ).hex
            source_key = delivery.get("source_key") or ""
            if source_key.startswith("review-start:") and await db.events.review_finished(
                source_key
            ):
                await db.outbox.set_status(event_id, recipient, "skipped")
                result.skipped.append(recipient)
                continue
            target = delivery["target_entity"]
            session = resolve_entity_session(target, config)
            if session is None:
                await db.outbox.set_status(event_id, recipient, "skipped")
                result.skipped.append(recipient)
                continue
            delivery["session_name"] = session
            try:
                if delivery["delivery_kind"] == "issue" and await is_acknowledged(
                    db, delivery["repo"], delivery["issue_number"], target, session
                ):
                    await db.outbox.set_status(event_id, recipient, "skipped")
                    result.skipped.append(recipient)
                    continue
                if retry:
                    issue, retired = await current_notification_issue(
                        gh, delivery["repo"], delivery["issue_number"], source_key=source_key
                    )
                    if retired or (
                        delivery["delivery_kind"] == "issue"
                        and target not in route_issue(issue, EventType.ISSUE_OPENED, config).queue
                    ):
                        await db.outbox.set_status(event_id, recipient, "skipped")
                        result.skipped.append(recipient)
                        continue
                    if delivery["enforce_issue_queue"]:
                        delivery["queue_scope"] = queue_scope(
                            await list_open_queue_for_target(config, target, gh, db=db)
                        )

                report = await safe_deliver(
                    **delivery,
                    config=config,
                    db=db,
                    source="github-outbox",
                    event_id=event_id,
                )
                # Receipt persistence stays after the serialized delivery has
                # released its locks, including every early-return receipt.
                if not report.unconfirmed and report.outcome in {
                    DeliveryOutcome.DELIVERED,
                    DeliveryOutcome.ALREADY_DELIVERED,
                }:
                    status = "delivered"
                elif report.queued:
                    status = "queued"
                else:
                    status = "failed"
                await db.outbox.set_status(event_id, recipient, status)
                outcome = report.outcome
                if outcome == DeliveryOutcome.DELIVERED:
                    result.delivered.append(session)
                elif outcome == DeliveryOutcome.ALREADY_DELIVERED:
                    result.skipped.append(session)
                elif outcome in {DeliveryOutcome.OFFLINE, DeliveryOutcome.DELIVERY_FAILED}:
                    result.offline.append(session)
                else:
                    result.deferred.append(session)
            except Exception:
                await db.outbox.set_status(event_id, recipient, "failed")
                result.offline.append(session)
                log.exception("Outbox event %s delivery to %s failed", event_id, recipient)
        return result


async def retry_outbox(
    config: BackboneConfig, db: BackboneDB, gh: GitHubClient | None
) -> dict[str, int]:
    """Resume durable fan-outs even when their original webhook is not replayed."""
    summary: dict[str, int] = {}
    for event_id in await db.outbox.pending_events():
        try:
            result = await flush_outbox(event_id, config, db, gh)
            if await db.outbox.finish_event(event_id, "outbox: recipients delivered or queued"):
                summary["outbox_completed"] = summary.get("outbox_completed", 0) + 1
            if result.delivered:
                summary["outbox_delivered"] = summary.get("outbox_delivered", 0) + len(
                    result.delivered
                )
        except Exception:
            log.exception("Could not retry outbox event %s", event_id)
            summary["outbox_errors"] = summary.get("outbox_errors", 0) + 1
    return summary
