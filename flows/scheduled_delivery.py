"""Scheduled delivery flow: deliver a specific issue at a scheduled time.

Used for deferred deliveries — when an agent was busy at dispatch time,
this flow can be scheduled to deliver later.
"""

from __future__ import annotations

import logging

from prefect import flow

from src.agent_state import get_agent_state, should_deliver
from src.config import BackboneConfig
from src.github import GitHubClient
from src.notifications import format_next_issue_notification
from src.persistence import BackboneDB
from src.tmux import send_message, session_exists

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

    # Check if session is online
    if not await session_exists(session_name):
        return "offline"

    # Check agent state
    state_snap = await get_agent_state(
        config.agent_state.state_path,
        session_name,
        config.agent_state.stale_threshold_seconds,
    )
    if not should_deliver(state_snap.state, is_blocking):
        return "busy"

    # Fetch current issue state
    async with GitHubClient(config) as gh:
        issue = await gh.get_issue(issue_number)

    if issue.state == "closed":
        return "issue_closed"

    # Deliver
    message = format_next_issue_notification(issue)
    if await send_message(session_name, message):
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

    return "delivery_failed"
