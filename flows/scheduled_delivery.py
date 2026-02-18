"""Scheduled delivery flow: deliver a specific issue at a scheduled time.

Used for deferred deliveries — when an agent was busy at dispatch time,
this flow can be scheduled to deliver later.
"""

from __future__ import annotations

import logging

from prefect import flow

from src.config import BackboneConfig
from src.github import GitHubClient
from src.notifications import format_next_issue_notification
from src.persistence import BackboneDB
from src.session_bridge import safe_deliver

log = logging.getLogger(__name__)


@flow(name="scheduled-delivery")
async def scheduled_delivery(
    issue_number: int,
    target_entity: str,
    session_name: str,
    is_blocking: bool = False,
) -> str:
    """Deliver a specific issue to a specific session.

    Returns outcome string: delivered, offline, busy, issue_closed.
    """
    config = BackboneConfig.from_toml()

    # Fetch current issue state
    async with GitHubClient(config) as gh:
        issue = await gh.get_issue(issue_number)

    if issue.state == "closed":
        return "issue_closed"

    # Deliver via safe_deliver (handles state checks)
    message = format_next_issue_notification(issue)
    outcome = await safe_deliver(
        session_name,
        message,
        config,
        issue_number=issue_number,
        target_entity=target_entity,
        flow_name="scheduled-delivery",
        priority=is_blocking,
    )

    if outcome == "delivered":
        # Record delivery
        async with BackboneDB(str(config.delivery.db_file)) as db:
            await db.record_delivery(
                issue_number=issue_number,
                target_entity=target_entity,
                session_name=session_name,
                outcome="delivered",
                flow_name="scheduled-delivery",
            )
        log.info("Scheduled delivery of #%d to %s", issue_number, session_name)
        return "delivered"

    if outcome == "offline":
        return "offline"
    busy_states = ("agent_working", "plan_waiting", "copy_mode", "user_interacting", "grace_period")
    if outcome in busy_states:
        return "busy"
    return "delivery_failed"
