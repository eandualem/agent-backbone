"""Dependency tracker: detect when all sub-issues of a parent are resolved.

On issue_closed, checks if the closed issue is a sub-issue of any parent.
If all sub-issues of a parent are now closed, notifies the parent's targets.
"""

from __future__ import annotations

import logging

from prefect import flow, task

from src.config import BackboneConfig
from src.github import GitHubClient
from src.notifications import format_unblock_notification
from src.persistence import BackboneDB
from src.session_bridge import safe_deliver

log = logging.getLogger(__name__)


@task
async def check_parent_resolved(config: BackboneConfig, parent_number: int) -> dict | None:
    """Check if all sub-issues of a parent are resolved.

    Returns parent issue data + notification targets if unblocked, None otherwise.
    """
    async with GitHubClient(config) as gh:
        sub_issues = await gh.get_sub_issues(parent_number)
        if not sub_issues:
            return None

        all_resolved = all(si.state == "closed" for si in sub_issues)
        if not all_resolved:
            return None

        parent = await gh.get_issue(parent_number)
        return {
            "parent": parent,
            "targets": parent.labels.targets,
        }


@flow(name="dependency-tracker")
async def on_dependency_resolved(closed_issue_number: int) -> dict:
    """Check if closing this issue unblocks any parent issues.

    Returns a dict mapping parent_number → outcome.
    """
    config = BackboneConfig.from_toml()
    result: dict[str, str] = {"parents_checked": "0"}

    async with BackboneDB(str(config.delivery.db_file)) as db:
        parents = await db.get_parents(closed_issue_number)

    if not parents:
        return result

    result["parents_checked"] = str(len(parents))

    for parent_num in parents:
        resolved = await check_parent_resolved(config, parent_num)
        if not resolved:
            result[f"parent_{parent_num}"] = "still_blocked"
            continue

        parent_issue = resolved["parent"]
        message = format_unblock_notification(parent_issue)
        delivered_to: list[str] = []

        for target in resolved["targets"]:
            if target in config.entities.skip:
                continue
            session_name = config.entities.sessions.get(target)
            if not session_name:
                continue
            outcome = await safe_deliver(session_name, message, config)
            if outcome == "delivered":
                delivered_to.append(session_name)

        if delivered_to:
            result[f"parent_{parent_num}"] = f"unblocked_delivered_to:{','.join(delivered_to)}"
        else:
            result[f"parent_{parent_num}"] = "unblocked_no_delivery"
        log.info(
            "Parent #%d unblocked — all sub-issues resolved. Notified: %s",
            parent_num,
            delivered_to,
        )

    return result
