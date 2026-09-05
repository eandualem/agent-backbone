"""Sub-issue dependency tracking (per repository)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_backbone.models import DeliveryOutcome
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import format_unblock_notification
from agent_backbone.services.routing._resolution import resolve_entity_session
from agent_backbone.services.routing._targets import list_open_queue_for_target

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)

SOURCE = "dependency-tracker"


async def check_parent_resolved(
    config: BackboneConfig, parent_number: int, gh: GitHubClient, *, repo: str = ""
) -> dict | None:
    """Parent issue + targets if every sub-issue is closed, else None."""
    sub_issues = await gh.get_sub_issues(parent_number, repo_full_name=repo) or []
    if not sub_issues or not all(si.state == "closed" for si in sub_issues):
        return None
    parent = await gh.get_issue(parent_number, repo_full_name=repo)
    return {"parent": parent, "targets": parent.labels.targets}


async def on_dependency_resolved(
    closed_issue_number: int,
    repo: str,
    config: BackboneConfig,
    db: BackboneDB,
    gh: GitHubClient,
) -> dict:
    """If closing this issue unblocks a parent, tell the parent's targets."""
    result: dict[str, str] = {"parents_checked": "0"}
    parents = await db.dependencies.parents(closed_issue_number, repo=repo)
    if not parents:
        return result
    result["parents_checked"] = str(len(parents))

    for parent_num in parents:
        resolved = await check_parent_resolved(config, parent_num, gh, repo=repo)
        if not resolved:
            result[f"parent_{parent_num}"] = "still_blocked"
            continue
        message = format_unblock_notification(resolved["parent"])
        delivered_to: list[str] = []
        for target in resolved["targets"]:
            session_name = resolve_entity_session(target, config)
            if session_name is None:
                continue
            outcome = await safe_deliver(
                session_name,
                message,
                config,
                db=db,
                repo=repo,
                issue_number=parent_num,
                target_entity=target,
                source=SOURCE,
                delivery_kind="watch",
            )
            if outcome == DeliveryOutcome.DELIVERED:
                delivered_to.append(session_name)
        result[f"parent_{parent_num}"] = (
            f"unblocked_delivered_to:{','.join(delivered_to)}"
            if delivered_to
            else "unblocked_no_delivery"
        )
    return result


async def sync_dependencies(
    config: BackboneConfig, db: BackboneDB, gh: GitHubClient | None
) -> None:
    """Record sub-issue relationships for every open issue in every agent queue."""
    if gh is None:
        return
    checked: set[tuple[str, int]] = set()
    for name in config.agents.names:
        for issue in await list_open_queue_for_target(config, name, gh):
            key = (issue.repo_full_name, issue.number)
            if key in checked:
                continue
            checked.add(key)
            subs = await gh.get_sub_issues(issue.number, repo_full_name=issue.repo_full_name)
            if subs is not None:  # an empty answer clears stale edges; a failed fetch keeps them
                await db.dependencies.sync(
                    issue.number, [s.number for s in subs], repo=issue.repo_full_name
                )
