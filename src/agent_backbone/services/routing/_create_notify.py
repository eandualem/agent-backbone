"""Create-and-notify — issue creation with direct terminal notification."""

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
from agent_backbone.services.routing._targets import list_open_queue_for_target

log = logging.getLogger(__name__)


async def create_and_notify(
    gh: GitHubClient,
    title: str,
    body: str,
    labels: list[str],
    config: BackboneConfig,
    *,
    db: BackboneDB | None = None,
    flow_name: str = "",
) -> IssueData:
    """Create a GitHub issue and directly notify target agents.

    Bypasses the webhook roundtrip by dispatching terminal notifications
    immediately after issue creation, using the same notification format as
    the webhook dispatcher.
    """
    targets = [label.removeprefix("for:") for label in labels if label.startswith("for:")]
    validate_issue_targets(targets, config)

    issue = await gh.create_issue(title, body, labels)

    if not targets:
        log.info("Created issue #%d with no for: targets — skipping notification", issue.number)
        return issue

    message = format_issue_notification(issue)

    for target in targets:
        session_names = resolve_entity_sessions(target, config)
        if not session_names:
            log.info("No session for target '%s' on #%d — skipping", target, issue.number)
            continue

        queue_scope_issue_numbers = {
            item.number
            for item in await list_open_queue_for_target(
                config,
                target,
                gh,
                issue_repo_full_name=issue.repo_full_name,
            )
        }
        for session_name in session_names:
            outcome = await safe_deliver(
                session_name,
                message,
                config,
                db=db,
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
