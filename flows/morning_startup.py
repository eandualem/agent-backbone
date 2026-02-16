"""Morning startup routine: start agents, deliver overnight issues, send summary.

Scheduled via cron (default 08:00 local time). Starts configured morning agents,
checks for issues that accumulated overnight, and sends a Telegram digest.
"""

from __future__ import annotations

import logging

from prefect import flow, task

from src.config import BackboneConfig
from src.github import GitHubClient
from src.notifications import format_digest
from src.tmux import list_sessions, send_message, start_session

log = logging.getLogger(__name__)


@task
async def start_morning_agents(config: BackboneConfig) -> list[str]:
    """Start configured morning agents. Returns list of started sessions."""
    started = []
    for agent in config.daily_routines.morning_agents:
        session_name = config.entities.sessions.get(agent, agent)
        if await start_session(session_name):
            started.append(session_name)
    return started


@task
async def count_pending_issues(
    config: BackboneConfig,
) -> dict[str, int]:
    """Count pending issues per entity."""
    counts: dict[str, int] = {}
    async with GitHubClient(config) as gh:
        for entity in config.entities.all_entities:
            label = f"for:{entity}"
            issues = await gh.list_open_issues(label)
            if issues:
                counts[entity] = len(issues)
    return counts


@task
async def deliver_overnight_issues(
    config: BackboneConfig, pending: dict[str, int]
) -> dict[str, str]:
    """Deliver the highest-priority pending issue to each online agent."""
    results: dict[str, str] = {}
    sessions = set(await list_sessions())

    async with GitHubClient(config) as gh:
        for entity in config.entities.all_entities:
            session = config.entities.sessions.get(entity)
            if not session or session not in sessions:
                continue
            if entity not in pending:
                continue

            label = f"for:{entity}"
            issues = await gh.list_open_issues(label)
            if issues:
                from src.notifications import format_next_issue_notification

                msg = format_next_issue_notification(issues[0])
                if await send_message(session, msg):
                    results[entity] = f"delivered_#{issues[0].number}"
                else:
                    results[entity] = "delivery_failed"

    return results


@flow(name="morning-startup")
async def morning_startup() -> dict:
    """Execute morning startup routine.

    1. Start configured morning agents
    2. Count pending issues per entity
    3. Deliver overnight issues to online agents
    4. Return summary for Telegram digest
    """
    config = BackboneConfig.from_toml()

    started = await start_morning_agents(config)
    pending = await count_pending_issues(config)
    deliveries = await deliver_overnight_issues(config, pending)
    sessions = await list_sessions()

    digest = format_digest(
        title="Morning Startup",
        sessions=sessions,
        pending_counts=pending,
        notes=[f"Started: {', '.join(started)}"] if started else None,
    )
    log.info(digest)

    return {
        "started": started,
        "pending": pending,
        "deliveries": deliveries,
        "digest": digest,
    }
