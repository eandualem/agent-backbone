"""Delivery retry flow: query failed deliveries, retry if target now online.

Runs on a configurable interval (default 5 minutes). Queries the persistence
layer for failed/offline/deferred deliveries and retries them if the target
agent is now online and not busy.
"""

from __future__ import annotations

import logging

from prefect import flow, task

from src.agent_state import get_agent_state, should_deliver
from src.config import BackboneConfig
from src.github import GitHubClient
from src.notifications import format_next_issue_notification
from src.persistence import BackboneDB
from src.tmux import send_message, session_exists

log = logging.getLogger(__name__)


@task
async def retry_delivery(config: BackboneConfig, delivery: dict) -> str:
    """Attempt to retry a single failed delivery.

    Returns outcome string: retried, still_offline, still_busy, issue_closed.
    """
    session_name = delivery["session_name"]
    issue_number = delivery["issue_number"]
    target_entity = delivery["target_entity"]

    # Check if session is online
    if not await session_exists(session_name):
        return "still_offline"

    # Check agent state
    state_snap = await get_agent_state(
        config.agent_state.state_path,
        session_name,
        config.agent_state.stale_threshold_seconds,
    )
    if not should_deliver(state_snap.state, require_idle=True):
        return "still_busy"

    # Fetch current issue state from GitHub
    async with GitHubClient(config) as gh:
        try:
            issue = await gh.get_issue(issue_number)
        except Exception:
            log.warning("Failed to fetch issue #%d for retry", issue_number)
            return "fetch_failed"

    # Skip if issue is now closed
    if issue.state == "closed":
        return "issue_closed"

    # Deliver
    message = format_next_issue_notification(issue)
    if await send_message(session_name, message):
        log.info(
            "Retry delivered #%d to %s (%s)",
            issue_number,
            target_entity,
            session_name,
        )
        return "retried"
    return "delivery_failed"


@flow(name="delivery-retry")
async def delivery_retry() -> dict:
    """Retry failed deliveries for agents that are now online.

    Returns summary of retry outcomes.
    """
    config = BackboneConfig.from_toml()
    summary: dict[str, int] = {}

    async with BackboneDB(str(config.delivery.db_file)) as db:
        failed = await db.get_failed_deliveries(limit=20)

    if not failed:
        log.info("No failed deliveries to retry")
        return summary

    log.info("Found %d failed deliveries to retry", len(failed))

    for delivery in failed:
        outcome = await retry_delivery(config, delivery)
        summary[outcome] = summary.get(outcome, 0) + 1

        # Record the retry attempt
        if outcome == "retried":
            async with BackboneDB(str(config.delivery.db_file)) as db:
                await db.record_delivery(
                    issue_number=delivery["issue_number"],
                    target_entity=delivery["target_entity"],
                    session_name=delivery["session_name"],
                    outcome="retried",
                    flow_name="delivery-retry",
                )

    log.info("Retry complete: %s", summary)
    return summary
