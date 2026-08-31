"""Event ingestion — one entry point for webhook and polled GitHub events."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.models import EventType, IssueEvent
from agent_backbone.services.routing._targets import resolve_event_targets

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient
    from agent_backbone.services.routing.interface import DeliveryService, DispatchService

log = logging.getLogger(__name__)


async def dispatch_event(
    event: IssueEvent,
    config: BackboneConfig,
    db: BackboneDB,
    gh: GitHubClient | None,
    delivery_svc: DeliveryService,
    dispatch_svc: DispatchService,
) -> str:
    """Route a normalized event to the lifecycle or dispatch handler.

    Returns a short outcome string (``dispatch: …``, ``lifecycle: …``,
    ``deduped: …``, ``ignored: …``) used for logging and HTTP responses.
    """
    if event.delivery_id:
        try:
            await db.record_delivery_id(event.delivery_id)
        except Exception:
            log.warning("Failed to persist delivery ID")

    if event.event_type == EventType.ISSUE_CLOSED:
        if gh is None:
            return "ignored: github client not configured"
        result = await dispatch_svc.on_issue_closed(event, config, gh, db)
        return f"lifecycle: {result}"

    # Comments on closed issues cannot be actionable and would loop in the queue.
    if event.event_type == EventType.COMMENT_CREATED and event.issue.state == "closed":
        log.info("Ignoring comment on closed issue #%d", event.issue.number)
        return f"ignored: comment on closed issue #{event.issue.number}"

    if event.event_type in (
        EventType.ISSUE_OPENED,
        EventType.ISSUE_LABELED,
        EventType.COMMENT_CREATED,
        EventType.PULL_REQUEST_OPENED,
    ):
        if event.event_type in (EventType.ISSUE_OPENED, EventType.ISSUE_LABELED):
            targets = resolve_event_targets(event, config)
            if targets and all(
                delivery_svc.is_recent_notification(event.issue.number, t) for t in targets
            ):
                log.info(
                    "Dedup: #%d reason=all_targets_recently_notified targets=%s",
                    event.issue.number,
                    targets,
                )
                return f"deduped: all targets already notified for #{event.issue.number}"

        result = await dispatch_svc.issue_dispatcher(event, config, db, gh)
        return (
            f"dispatch: {len(result.delivered)} delivered, "
            f"{len(result.offline)} offline, "
            f"{len(result.deferred)} deferred"
        )

    return f"ignored: {event.event_type}"
