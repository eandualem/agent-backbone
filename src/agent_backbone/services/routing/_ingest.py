"""Event ingestion — one entry point for webhook and polled GitHub events.

Every event is stored in the ``events`` table before routing (that is the
activity feed and the dedup record), then handed to the lifecycle or
dispatch handler, and marked with its outcome.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.models import EventType, IssueEvent
from agent_backbone.services.routing._targets import issue_repo, route_issue_event

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient
    from agent_backbone.services.routing.interface import DeliveryService, DispatchService

log = logging.getLogger(__name__)


def _source(delivery_id: str) -> str:
    return "poll" if delivery_id.startswith("poll:") else "webhook"


def _dedup_id(event: IssueEvent) -> str:
    """The events-table dedup key — source-independent where possible.

    Webhook and poll synthesise different delivery ids for the same comment,
    so comments dedup on repo + comment id instead of the transport's id.
    """
    if event.event_type == EventType.COMMENT_CREATED and event.comment and event.comment.id:
        return f"comment:{issue_repo(event.issue)}:{event.comment.id}"
    return event.delivery_id


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
                delivery_id=_dedup_id(event),
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


# Called with (repo, issue_number) after an issue-closed event is processed.
# Wired by the application layer (e.g. swarm teardown) to avoid routing
# importing higher-level services.
_issue_closed_listeners: list = []


def register_issue_closed_listener(listener) -> None:
    """Register an async ``(repo, issue_number)`` callback for closed issues."""
    _issue_closed_listeners.append(listener)


async def _route(event, config, db, gh, delivery_svc, dispatch_svc) -> str:
    if event.event_type == EventType.ISSUE_CLOSED:
        if gh is None:
            return "ignored: github client not configured"
        result = await dispatch_svc.on_issue_closed(event, config, gh, db)
        for listener in _issue_closed_listeners:
            try:
                await listener(issue_repo(event.issue), event.issue.number)
            except Exception:
                log.exception("issue-closed listener failed (non-fatal)")
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
            targets = route_issue_event(event, config).queue
            repo = issue_repo(event.issue)
            window = config.routing.notification_dedup_seconds
            if targets and all(
                delivery_svc.is_recent_notification(repo, event.issue.number, t, window)
                for t in targets
            ):
                return f"deduped: all targets already notified for #{event.issue.number}"

        result = await dispatch_svc.issue_dispatcher(event, config, db, gh)
        return (
            f"dispatch: {len(result.delivered)} delivered, "
            f"{len(result.offline)} offline, "
            f"{len(result.deferred)} deferred"
        )

    return f"ignored: {event.event_type}"
