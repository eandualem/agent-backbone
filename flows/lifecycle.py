"""Close-then-next flow: issue closed → query GitHub → deliver next.

When an issue is closed, determines which entity was the target,
queries GitHub for remaining open issues, and delivers the next one.
"""

from __future__ import annotations

import logging

from prefect import flow, task

from src.config import ENTITY_SESSIONS, FALLBACK_ROUTING, SKIP_ENTITIES, BackboneConfig
from src.github import GitHubClient
from src.models import IssueData, IssueEvent
from src.notifications import format_next_issue_notification
from src.tmux import send_message, session_exists

log = logging.getLogger(__name__)


@task
async def find_next_issue(config: BackboneConfig, entity: str) -> IssueData | None:
    """Query GitHub for the next open issue targeting an entity.

    Returns the highest-priority issue (blocking first, then oldest).
    """
    label = f"for:{entity}"
    async with GitHubClient(config) as gh:
        issues = await gh.list_open_issues(label)

    if not issues:
        log.info("No remaining open issues for %s", entity)
        return None

    log.info("Found %d open issue(s) for %s, next: #%d", len(issues), entity, issues[0].number)
    return issues[0]


@task
async def deliver_next(session_name: str, issue: IssueData) -> bool:
    """Deliver a next-issue notification to a tmux session."""
    message = format_next_issue_notification(issue)
    return await send_message(session_name, message)


@flow(name="issue-lifecycle")
async def on_issue_closed(event: IssueEvent) -> dict:
    """Handle an issue_closed event by delivering the next queued issue.

    For each entity that was a target of the closed issue:
    1. Query GitHub for remaining open issues with that entity's for: label
    2. If issues remain and entity session is online, deliver the next one
    """
    result: dict[str, str] = {}  # entity → outcome
    config = BackboneConfig()

    for target in event.issue.labels.targets:
        if target in SKIP_ENTITIES:
            result[target] = "skipped"
            continue

        # Resolve session name
        if target == "coding-agent":
            session_name = FALLBACK_ROUTING.get(target, "ike")
        else:
            session_name = ENTITY_SESSIONS.get(target)

        if not session_name:
            result[target] = "no_session"
            continue

        # Check if session is online
        if not await session_exists(session_name):
            log.info("Session '%s' offline — next issue will be delivered when online", session_name)
            result[target] = "offline"
            continue

        # Find the next issue
        next_issue = await find_next_issue(config, target)
        if not next_issue:
            result[target] = "queue_empty"
            continue

        # Deliver it
        if await deliver_next(session_name, next_issue):
            result[target] = f"delivered_#{next_issue.number}"
        else:
            result[target] = "delivery_failed"

    log.info("Lifecycle complete: %s", result)
    return result
