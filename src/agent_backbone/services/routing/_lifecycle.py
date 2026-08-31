"""Close-then-next: issue closed -> deliver the next issue to each target,
tell the opener, and check whether a parent issue is now unblocked."""

from __future__ import annotations

import logging

from agent_backbone.config import BackboneConfig
from agent_backbone.models import EventType, IssueData, IssueEvent
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.routing._dedup import is_recent_notification
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import (
    format_closed_notification,
    format_next_issue_notification,
)
from agent_backbone.services.routing._resolution import resolve_entity_session
from agent_backbone.services.routing._targets import (
    issue_repo,
    list_open_queue_for_target,
    queue_scope,
    route_issue_event,
)
from agent_backbone.services.terminal import session_exists

log = logging.getLogger(__name__)


async def _check_dependencies(
    issue_number: int, repo: str, config: BackboneConfig, db: BackboneDB, gh: GitHubClient
) -> None:
    try:
        from agent_backbone.services.routing._dependencies import on_dependency_resolved

        await on_dependency_resolved(issue_number, repo, config, db, gh)
    except Exception:
        log.exception("Dependency tracker failed for %s#%d (non-fatal)", repo, issue_number)


async def find_next_issue(
    config: BackboneConfig,
    entity: str,
    gh: GitHubClient,
    exclude: tuple[str, int] | None = None,
) -> IssueData | None:
    """Highest-priority open issue in an agent's queue, excluding the just-closed one."""
    issues = await list_open_queue_for_target(config, entity, gh)
    if exclude is not None:
        issues = [
            i
            for i in issues
            if not (i.number == exclude[1] and issue_repo(i).casefold() == exclude[0].casefold())
        ]
    if not issues:
        return None
    return issues[0]


async def deliver_next(
    session_name: str,
    issue: IssueData,
    config: BackboneConfig,
    db: BackboneDB | None = None,
    target_entity: str = "",
    scope: set[tuple[str, int]] | None = None,
) -> str:
    return await safe_deliver(
        session_name,
        format_next_issue_notification(issue),
        config,
        db=db,
        repo=issue_repo(issue),
        issue_number=issue.number,
        target_entity=target_entity,
        flow_name="issue-lifecycle",
        enforce_issue_queue=True,
        queue_scope=scope,
    )


async def on_issue_closed(
    event: IssueEvent,
    config: BackboneConfig,
    gh: GitHubClient,
    db: BackboneDB | None = None,
) -> dict:
    """Handle an issue_closed event."""
    result: dict[str, str] = {}
    repo = issue_repo(event.issue)
    closed = (repo, event.issue.number)

    if db is not None:
        try:
            purged = await db.purge_pending_for_issue(event.issue.number, repo=repo)
            if purged:
                log.info(
                    "Purged %d queued messages for closed %s#%d", purged, repo, event.issue.number
                )
        except Exception:
            log.exception("Failed to purge queue for %s#%d (non-fatal)", repo, event.issue.number)

    routing = route_issue_event(
        IssueEvent(event_type=EventType.ISSUE_OPENED, issue=event.issue), config
    )

    for target in routing.queue:
        session_name = resolve_entity_session(target, config)
        if not session_name:
            result[target] = "no_session"
            continue
        if not await session_exists(session_name):
            result[target] = "offline"
            continue

        next_issue = await find_next_issue(config, target, gh, exclude=closed)
        if not next_issue:
            result[target] = "queue_empty"
            continue

        scope = queue_scope(await list_open_queue_for_target(config, target, gh))
        scope.discard(closed)
        if is_recent_notification(
            next_issue.number, session_name, config.routing.notification_dedup_seconds
        ):
            result[target] = f"deduped_#{next_issue.number}"
            continue

        outcome = await deliver_next(
            session_name, next_issue, config, db, target_entity=target, scope=scope
        )
        result[target] = f"delivered_#{next_issue.number}" if outcome == "delivered" else outcome

    # Tell the opener (from:) that their issue was closed, unless they were a target.
    sender = event.issue.labels.sender
    if sender and sender in config.agents and sender not in routing.queue:
        session_name = resolve_entity_session(sender, config)
        if session_name:
            outcome = await safe_deliver(
                session_name,
                format_closed_notification(event.issue),
                config,
                db=db,
                repo=repo,
                issue_number=event.issue.number,
                target_entity=sender,
                flow_name="issue-lifecycle",
                delivery_kind="watch",
            )
            result[f"opener:{sender}"] = outcome

    if db is not None:
        await _check_dependencies(event.issue.number, repo, config, db, gh)

    log.info("Lifecycle %s#%d: %s", repo, event.issue.number, result)
    return result
