"""Event ingestion — one entry point for webhook and polled GitHub events.

Every event is stored in the ``events`` table before routing (that is the
activity feed and the dedup record), then handed to the lifecycle or
dispatch handler, and marked with its outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from agent_backbone.models import EventType, IssueEvent
from agent_backbone.services.routing._lifecycle import on_issue_closed
from agent_backbone.services.routing._router import issue_dispatcher

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)

IssueClosedHook = Callable[[str, int], Awaitable[None]]
"""``(repo, issue_number)`` callback run after a closed issue is processed —
the application layer wires swarm teardown through this so routing never
imports the packages above it."""

_in_flight: set[str] = set()
"""Delivery ids being routed right now. The events table deduplicates
durably; this closes the window in which a retry of the same delivery
arrives before the first copy is marked processed."""


def _source(delivery_id: str) -> str:
    return "poll" if delivery_id.startswith("poll:") else "webhook"


def _dedup_id(event: IssueEvent) -> str:
    """The events-table dedup key — source-independent where possible.

    Webhook and poll synthesise different delivery ids for the same comment,
    so comments dedup on repo + comment id instead of the transport's id.
    """
    if event.event_type == EventType.COMMENT_CREATED and event.comment and event.comment.id:
        return f"comment:{event.issue.repo_full_name}:{event.comment.id}"
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
    *,
    issue_closed_hooks: Sequence[IssueClosedHook] = (),
) -> str:
    """Store, route and mark an event. Returns a short outcome string."""
    key = _dedup_id(event)
    if key and key in _in_flight:
        return f"deduped: event {event.delivery_id} is being routed"
    _in_flight.add(key)
    try:
        return await _store_route_mark(event, key, config, db, gh, issue_closed_hooks)
    finally:
        _in_flight.discard(key)


async def _store_route_mark(event, key, config, db, gh, issue_closed_hooks) -> str:
    event_id: int | None = None
    if event.delivery_id:
        try:
            event_id = await db.events.record(
                delivery_id=key,
                source=_source(event.delivery_id),
                repo=event.issue.repo_full_name,
                event_type=event.event_type.value,
                issue_number=event.issue.number or None,
                sender=(event.comment.user_login if event.comment else event.issue.labels.sender),
                summary=_summary(event),
            )
            if event_id is None:
                return f"deduped: event {event.delivery_id} already stored"
        except Exception:
            log.warning("Failed to persist event %s (continuing)", event.delivery_id)

    outcome = await _route(event, config, db, gh, issue_closed_hooks)

    if event_id is not None:
        try:
            await db.events.mark_processed(event_id, outcome)
        except Exception:
            log.debug("Failed to mark event processed (non-fatal)")
    return outcome


_DISPATCHED = frozenset(
    {
        EventType.ISSUE_OPENED,
        EventType.ISSUE_LABELED,
        EventType.COMMENT_CREATED,
        EventType.PULL_REQUEST_OPENED,
    }
)


async def _route(event, config, db, gh, issue_closed_hooks) -> str:
    if event.event_type == EventType.ISSUE_CLOSED:
        if gh is None:
            return "ignored: github client not configured"
        result = await on_issue_closed(event, config, gh, db)
        for hook in issue_closed_hooks:
            try:
                await hook(event.issue.repo_full_name, event.issue.number)
            except Exception:
                log.exception("issue-closed hook failed (non-fatal)")
        return f"lifecycle: {result}"

    if event.event_type == EventType.COMMENT_CREATED and event.issue.state == "closed":
        return f"ignored: comment on closed issue #{event.issue.number}"

    if event.event_type in _DISPATCHED:
        result = await issue_dispatcher(event, config, db, gh)
        return (
            f"dispatch: {len(result.delivered)} delivered, "
            f"{len(result.offline)} offline, "
            f"{len(result.deferred)} deferred"
        )

    return f"ignored: {event.event_type}"
