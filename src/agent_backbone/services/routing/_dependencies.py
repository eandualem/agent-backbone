"""Dependency tracking: sub-issue resolution detection and sync.

Detects when all sub-issues of a parent are resolved, and syncs sub-issue
relationships to the database for close-time lookups.
"""

from __future__ import annotations

import logging

from agent_backbone.config import BackboneConfig
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.routing._delivery import safe_deliver
from agent_backbone.services.routing._format import format_unblock_notification
from agent_backbone.services.routing._resolution import resolve_entity_sessions

log = logging.getLogger(__name__)


async def check_parent_resolved(
    config: BackboneConfig, parent_number: int, gh: object
) -> dict | None:
    """Check if all sub-issues of a parent are resolved.

    Returns parent issue data + notification targets if unblocked, None otherwise.
    """
    sub_issues = await gh.get_sub_issues(parent_number)
    if not sub_issues:
        return None

    if not all(si.state == "closed" for si in sub_issues):
        return None

    parent = await gh.get_issue(parent_number)
    return {"parent": parent, "targets": parent.labels.targets}


async def on_dependency_resolved(
    closed_issue_number: int,
    config: BackboneConfig,
    db: BackboneDB,
    gh: object,
) -> dict:
    """Check if closing this issue unblocks any parent issues.

    Returns a dict mapping parent_number -> outcome.
    """
    result: dict[str, str] = {"parents_checked": "0"}

    parents = await db.get_parents(closed_issue_number)
    if not parents:
        return result

    result["parents_checked"] = str(len(parents))

    for parent_num in parents:
        resolved = await check_parent_resolved(config, parent_num, gh)
        if not resolved:
            result[f"parent_{parent_num}"] = "still_blocked"
            continue

        parent_issue = resolved["parent"]
        message = format_unblock_notification(parent_issue)
        delivered_to: list[str] = []

        for target in resolved["targets"]:
            for session_name in resolve_entity_sessions(target, config):
                outcome = await safe_deliver(session_name, message, config, db=db)
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


async def sync_dependencies(config: BackboneConfig, db: BackboneDB, gh: object) -> None:
    """Sync sub-issue relationships to the database for close-time lookups."""
    if not config.github.enabled:
        return
    checked: set[int] = set()
    for name in config.agents.names:
        issues = await gh.list_open_issues(f"for:{name}")
        for issue in issues:
            if issue.number in checked:
                continue
            checked.add(issue.number)
            subs = await gh.get_sub_issues(issue.number)
            if subs:
                await db.sync_dependencies(issue.number, [s.number for s in subs])
