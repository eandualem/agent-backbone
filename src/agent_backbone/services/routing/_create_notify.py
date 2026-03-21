"""Create-and-notify — issue creation with direct tmux notification."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient

from agent_backbone.models import IssueData
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import format_issue_notification
from agent_backbone.services.routing._resolution import (
    resolve_entity_sessions,
    validate_issue_targets,
)

log = logging.getLogger(__name__)


async def create_and_notify(
    gh: GitHubClient,
    title: str,
    body: str,
    labels: list[str],
    config: BackboneConfig,
    *,
    repo_full_name: str | None = None,
    db: BackboneDB | None = None,
    flow_name: str = "",
) -> IssueData:
    """Create a GitHub issue and directly notify target entities.

    Bypasses the webhook roundtrip by dispatching tmux notifications
    immediately after issue creation. Uses the same notification format
    as the webhook dispatcher (format_issue_notification).

    Args:
        gh: GitHub client (must already be started).
        title: issue title.
        body: issue body (markdown).
        labels: label names (e.g. ["from:coding-agent", "for:brunel", "task"]).
        config: backbone configuration.
        db: optional persistence layer for delivery queuing.
        flow_name: originating flow for logging/tracking.

    Returns:
        The created IssueData.
    """
    # Extract target entities from for: labels
    targets = [label.removeprefix("for:") for label in labels if label.startswith("for:")]
    validate_issue_targets(targets, config)

    issue = await gh.create_issue(title, body, labels, repo_full_name=repo_full_name)

    if not targets:
        log.info("Created issue #%d with no for: targets — skipping notification", issue.number)
        return issue

    message = format_issue_notification(issue)

    for target in targets:
        session_names = await resolve_entity_sessions(target, config, issue.title)
        if not session_names:
            log.info(
                "No session resolved for target '%s' on #%d — skipping",
                target,
                issue.number,
            )
            continue

        queue_scope_issue_numbers = {
            item.identity_key
            for item in await gh.list_open_issues(
                f"for:{target}",
                repo_full_name=issue.repo_full_name or None,
            )
        }
        for session_name in session_names:
            outcome = await safe_deliver(
                session_name,
                message,
                config,
                db=db,
                repo_full_name=issue.repo_full_name,
                issue_number=issue.number,
                target_entity=target,
                flow_name=flow_name,
                enforce_issue_queue=True,
                queue_scope_issue_numbers=queue_scope_issue_numbers,
            )
            log.info(
                "Direct notification for #%d → %s (%s): %s",
                issue.number,
                target,
                session_name,
                outcome,
            )

    return issue
