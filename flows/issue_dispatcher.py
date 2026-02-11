"""Core dispatch flow: event → parse → route → deliver.

Receives normalized IssueEvent from gateway, resolves target sessions,
and delivers notifications via tmux. Handles issue_opened, issue_labeled,
and comment_created events. issue_closed events are routed to lifecycle.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from prefect import flow, task

from src.config import (
    ENTITY_SESSIONS,
    FALLBACK_ROUTING,
    REPO_NAME_PATTERN,
    SKIP_ENTITIES,
)
from src.models import EventType, IssueEvent
from src.notifications import format_comment_notification, format_issue_notification
from src.tmux import send_message, session_exists

log = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    """Outcome of a dispatch operation."""

    delivered: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    offline: list[str] = field(default_factory=list)


@task
async def resolve_session(target: str, issue_title: str) -> str | None:
    """Resolve a target entity to a tmux session name.

    Named entities map directly. 'coding-agent' resolves by extracting
    repo name from issue title, falling back to Ike.
    """
    if target == "coding-agent":
        match = REPO_NAME_PATTERN.match(issue_title)
        if match:
            repo_name = match.group(1)
            if await session_exists(repo_name):
                log.info("Resolved coding-agent → repo session '%s'", repo_name)
                return repo_name
            log.info("Repo session '%s' not found, using fallback", repo_name)
        else:
            log.info("Could not extract repo name from title: %s", issue_title)
        fallback = FALLBACK_ROUTING.get(target)
        if fallback:
            log.info("Routing coding-agent → fallback '%s'", fallback)
            return fallback
        return None

    return ENTITY_SESSIONS.get(target)


@task
async def deliver_notification(session_name: str, message: str) -> bool:
    """Deliver a notification message to a tmux session."""
    return await send_message(session_name, message)


@flow(name="issue-dispatcher")
async def issue_dispatcher(event: IssueEvent) -> DispatchResult:
    """Dispatch a webhook event to target entity sessions.

    Routes issue_opened, issue_labeled, and comment_created events.
    issue_closed events should be routed to lifecycle.on_issue_closed instead.
    """
    result = DispatchResult()

    # Format the message based on event type
    if event.event_type in (EventType.ISSUE_OPENED, EventType.ISSUE_LABELED):
        message = format_issue_notification(event.issue)
    elif event.event_type == EventType.COMMENT_CREATED and event.comment:
        message = format_comment_notification(event.issue, event.comment)
    else:
        log.info("Ignoring event type: %s", event.event_type)
        return result

    # Deliver to each target
    for target in event.issue.labels.targets:
        if target in SKIP_ENTITIES:
            result.skipped.append(target)
            continue

        session_name = await resolve_session(target, event.issue.title)
        if not session_name:
            log.warning("Could not resolve session for target '%s'", target)
            result.skipped.append(target)
            continue

        if await deliver_notification(session_name, message):
            result.delivered.append(session_name)
        else:
            result.offline.append(session_name)

    log.info(
        "Dispatch complete: %d delivered, %d skipped, %d offline",
        len(result.delivered),
        len(result.skipped),
        len(result.offline),
    )
    return result
