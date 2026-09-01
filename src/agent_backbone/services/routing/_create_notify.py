"""Create-and-notify — open an issue and deliver it without the webhook round trip."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.models import IssueData
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import format_issue_notification
from agent_backbone.services.routing._resolution import (
    resolve_entity_session,
    validate_issue_targets,
)
from agent_backbone.services.routing._targets import list_open_queue_for_target, queue_scope

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)


async def create_and_notify(
    gh: GitHubClient,
    title: str,
    body: str,
    labels: list[str],
    config: BackboneConfig,
    *,
    repo: str,
    db: BackboneDB | None = None,
    flow_name: str = "",
) -> IssueData:
    """Create ``repo#N`` and deliver it to its ``for:`` targets immediately."""
    targets = [label.removeprefix("for:") for label in labels if label.startswith("for:")]
    validate_issue_targets(targets, config)

    issue = await gh.create_issue(title, body, labels, repo_full_name=repo)
    if not targets:
        return issue

    message = format_issue_notification(issue)
    for target in targets:
        session_name = resolve_entity_session(target, config)
        if session_name is None:
            continue
        scope = queue_scope(await list_open_queue_for_target(config, target, gh))
        outcome = await safe_deliver(
            session_name,
            message,
            config,
            db=db,
            repo=repo,
            issue_number=issue.number,
            target_entity=target,
            flow_name=flow_name,
            enforce_issue_queue=True,
            queue_scope=scope,
        )
        log.info("Direct notification %s#%d → %s: %s", repo, issue.number, session_name, outcome)
    return issue
