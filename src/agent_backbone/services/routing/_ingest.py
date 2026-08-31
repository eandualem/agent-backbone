"""Event ingestion — one entry point for webhook and polled GitHub events.

Every event is stored in the ``events`` table before routing (that is the
activity feed and the dedup record), then handed to the lifecycle or
dispatch handler, and marked with its outcome.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.models import EventType, IssueEvent
from agent_backbone.services.routing._targets import issue_repo, resolve_event_targets

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient
    from agent_backbone.services.routing.interface import DeliveryService, DispatchService

log = logging.getLogger(__name__)


def _source(delivery_id: str) -> str:
    return "poll" if delivery_id.startswith("poll:") else "webhook"


def _summary(event: IssueEvent) -> str:
    title = event.issue.title[:120]
    if event.event_type == EventType.COMMENT_CREATED and event.comment:
        return f'comment on "{title}": {event.comment.body[:120]}'
    return f'{event.event_type.value}: "{title}"'


async def dispatch_event(
    event: IssueEvent,
    config: BackboneConfig,
    db: BackboneDB,
    gh: GitHubClient | None,
    delivery_svc: DeliveryService,
    dispatch_svc: DispatchService,
) -> str:
    """Store, route and mark an event. Returns a short outcome string."""
    event_id: int | None = None
    if event.delivery_id:
        try:
            event_id = await db.record_event(
                delivery_id=event.delivery_id,
                source=_source(event.delivery_id),
                repo=issue_repo(event.issue),
                event_type=event.event_type.value,
                issue_number=event.issue.number or None,
                sender=(event.comment.user_login if event.comment else event.issue.labels.sender),
                summary=_summary(event),
            )
            if event_id is None:
                return f"deduped: event {event.delivery_id} already stored"
        except Exception:
            log.warning("Failed to persist event %s (continuing)", event.delivery_id)

    outcome = await _route(event, config, db, gh, delivery_svc, dispatch_svc)

    if event_id is not None:
        try:
            await db.mark_event_processed(event_id, outcome)
        except Exception:
            log.debug("Failed to mark event processed (non-fatal)")
    return outcome


async def _route(event, config, db, gh, delivery_svc, dispatch_svc) -> str:
    if event.event_type == EventType.ISSUE_CLOSED:
        if gh is None:
            return "ignored: github client not configured"
        result = await dispatch_svc.on_issue_closed(event, config, gh, db)
        return f"lifecycle: {result}"

    if event.event_type == EventType.COMMENT_CREATED and event.issue.state == "closed":
        return f"ignored: comment on closed issue #{event.issue.number}"

    if event.event_type in (
        EventType.ISSUE_OPENED,
        EventType.ISSUE_LABELED,
        EventType.COMMENT_CREATED,
        EventType.PULL_REQUEST_OPENED,
    ):
        if event.event_type in (EventType.ISSUE_OPENED, EventType.ISSUE_LABELED):
            targets = resolve_event_targets(event, config)
            repo = issue_repo(event.issue)
            if targets and all(
                delivery_svc.is_recent_notification(repo, event.issue.number, t) for t in targets
            ):
                return f"deduped: all targets already notified for #{event.issue.number}"

        result = await dispatch_svc.issue_dispatcher(event, config, db, gh)
        return (
            f"dispatch: {len(result.delivered)} delivered, "
            f"{len(result.offline)} offline, "
            f"{len(result.deferred)} deferred"
        )

    return f"ignored: {event.event_type}"
