"""Close-then-next: issue closed -> query tracker -> deliver next.

When an issue is closed, determines which agents were its targets, queries
GitHub for their remaining open issues, and delivers the next one.
"""

from __future__ import annotations

import logging

from agent_backbone.config import BackboneConfig
from agent_backbone.models import IssueData, IssueEvent
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.routing._dedup import is_recent_notification
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import format_next_issue_notification
from agent_backbone.services.routing._resolution import resolve_entity_session
from agent_backbone.services.routing._targets import (
    default_repo_full_name,
    list_open_queue_for_target,
    resolve_event_targets,
)
from agent_backbone.services.terminal import session_exists

log = logging.getLogger(__name__)


def _issue_repo_full_name(issue: IssueData, config: BackboneConfig) -> str:
    """Resolve the source repository for an issue, defaulting to the main repo."""
    return issue.repo_full_name or default_repo_full_name(config)


async def _check_dependencies(
    issue_number: int, config: BackboneConfig, db: BackboneDB, gh: GitHubClient
) -> None:
    """Call dependency tracker — errors must not block lifecycle."""
    try:
        from agent_backbone.services.routing._dependencies import on_dependency_resolved

        await on_dependency_resolved(issue_number, config, db, gh)
    except Exception:
        log.exception("Dependency tracker failed for #%d (non-fatal)", issue_number)


async def find_next_issue(
    config: BackboneConfig,
    entity: str,
    gh: GitHubClient,
    exclude_number: int | None = None,
    repo_full_name: str | None = None,
) -> IssueData | None:
    """Query GitHub for the next open issue targeting an entity.

    Returns the highest-priority issue (blocking first, then oldest).
    Optionally excludes a specific issue number (e.g. a just-closed issue
    that may still appear as open due to GitHub eventual consistency).
    """
    issues = await list_open_queue_for_target(
        config,
        entity,
        gh,
        issue_repo_full_name=repo_full_name or "",
    )

    if exclude_number is not None:
        issues = [i for i in issues if i.number != exclude_number]

    if not issues:
        log.info("No remaining open issues for %s (exclude=%s)", entity, exclude_number)
        return None

    log.info("Found %d open issue(s) for %s, next: #%d", len(issues), entity, issues[0].number)
    return issues[0]


async def deliver_next(
    session_name: str,
    issue: IssueData,
    config: BackboneConfig,
    db: BackboneDB | None = None,
    target_entity: str = "",
    queue_scope_issue_numbers: set[int] | None = None,
) -> str:
    """Deliver a next-issue notification via safe_deliver."""
    message = format_next_issue_notification(issue)
    return await safe_deliver(
        session_name,
        message,
        config,
        db=db,
        issue_number=issue.number,
        target_entity=target_entity,
        flow_name="issue-lifecycle",
        enforce_issue_queue=True,
        queue_scope_issue_numbers=queue_scope_issue_numbers,
    )


async def on_issue_closed(
    event: IssueEvent,
    config: BackboneConfig,
    gh: GitHubClient,
    db: BackboneDB | None = None,
) -> dict:
    """Handle an issue_closed event by delivering the next queued issue.

    For each agent that was a target of the closed issue:
    1. Query GitHub for remaining open issues in that agent's queue
    2. If issues remain and the agent session is online, deliver the next one
    """
    result: dict[str, str] = {}

    # Purge any pending queue messages for the closed issue so they don't
    # loop in the retry cycle.
    if db is not None:
        try:
            purged = await db.purge_pending_for_issue(event.issue.number)
            if purged:
                log.info(
                    "Purged %d queued messages for closed issue #%d",
                    purged,
                    event.issue.number,
                )
        except Exception:
            log.exception("Failed to purge queue for issue #%d (non-fatal)", event.issue.number)

    repo_full_name = _issue_repo_full_name(event.issue, config)

    for target in resolve_event_targets(event, config):
        if target in config.routing.ignore_targets:
            result[target] = "skipped"
            continue

        session_name = resolve_entity_session(target, config)
        if not session_name:
            result[target] = "no_session"
            continue

        if not await session_exists(session_name):
            log.info("Session '%s' offline — next issue delivered when online", session_name)
            result[target] = "offline"
            continue

        next_issue = await find_next_issue(
            config,
            target,
            gh,
            exclude_number=event.issue.number,
            repo_full_name=repo_full_name,
        )
        if not next_issue:
            result[target] = "queue_empty"
            continue

        queue_scope_issue_numbers = {
            issue.number
            for issue in await list_open_queue_for_target(
                config,
                target,
                gh,
                issue_repo_full_name=repo_full_name,
            )
            if issue.number != event.issue.number
        }

        # Dedup: don't re-deliver an issue the agent was already notified about
        # recently (multiple closes in quick succession).
        if is_recent_notification(
            next_issue.number, session_name, config.routing.notification_dedup_seconds
        ):
            log.info(
                "Suppressed duplicate next-issue notification for #%d -> %s",
                next_issue.number,
                session_name,
            )
            result[target] = f"deduped_#{next_issue.number}"
            continue

        outcome = await deliver_next(
            session_name,
            next_issue,
            config,
            db,
            target_entity=target,
            queue_scope_issue_numbers=queue_scope_issue_numbers,
        )
        result[target] = f"delivered_#{next_issue.number}" if outcome == "delivered" else outcome

    if db is not None and repo_full_name == default_repo_full_name(config):
        await _check_dependencies(event.issue.number, config, db, gh)

    log.info("Lifecycle complete: %s", result)
    return result
