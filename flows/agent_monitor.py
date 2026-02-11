"""Periodic agent monitor: check sessions, deliver pending issues.

Runs on a 60-second interval. For each known entity, checks if their
tmux session is online and delivers pending issues that may have arrived
while the session was down.
"""

from __future__ import annotations

import logging

from prefect import flow, task

from src.config import ALL_ENTITIES, ENTITY_SESSIONS, BackboneConfig
from src.github import GitHubClient
from src.models import IssueData
from src.notifications import format_next_issue_notification
from src.tmux import list_sessions, send_message

log = logging.getLogger(__name__)


@task
async def check_pending_issues(config: BackboneConfig, entity: str) -> IssueData | None:
    """Check if an entity has pending open issues."""
    label = f"for:{entity}"
    async with GitHubClient(config) as gh:
        issues = await gh.list_open_issues(label)
    return issues[0] if issues else None


@flow(name="agent-monitor")
async def monitor_agents() -> dict:
    """Check all entity sessions and deliver pending issues to online agents.

    Returns a dict mapping entity → action taken.
    """
    result: dict[str, str] = {}
    config = BackboneConfig()

    active_sessions = set(await list_sessions())
    if not active_sessions:
        log.info("No tmux sessions active")
        return result

    for entity in ALL_ENTITIES:
        session_name = ENTITY_SESSIONS.get(entity)
        if not session_name or session_name not in active_sessions:
            continue

        # Check for pending issues
        next_issue = await check_pending_issues(config, entity)
        if not next_issue:
            result[entity] = "no_pending"
            continue

        # Deliver the oldest/highest-priority pending issue
        message = format_next_issue_notification(next_issue)
        if await send_message(session_name, message):
            result[entity] = f"delivered_#{next_issue.number}"
            log.info("Delivered pending issue #%d to %s", next_issue.number, entity)
        else:
            result[entity] = "delivery_failed"

    return result
