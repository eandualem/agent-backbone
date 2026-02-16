"""Evening shutdown routine: capture stats, identify stale sessions, send digest.

Scheduled via cron (default 18:00 local time). Does NOT stop sessions
automatically — just reports status for manual decision-making.
"""

from __future__ import annotations

import logging

from prefect import flow, task

from src.config import BackboneConfig
from src.github import GitHubClient
from src.notifications import format_digest
from src.persistence import BackboneDB
from src.tmux import list_sessions

log = logging.getLogger(__name__)


@task
async def capture_delivery_stats(config: BackboneConfig) -> dict:
    """Get delivery statistics for the day."""
    async with BackboneDB(str(config.delivery.db_file)) as db:
        recent = await db.query_deliveries(limit=100)
        failed = await db.get_failed_deliveries()

    outcomes: dict[str, int] = {}
    for d in recent:
        outcome = d["outcome"]
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    return {
        "total": len(recent),
        "failed": len(failed),
        "outcomes": outcomes,
    }


@task
async def count_pending_issues(config: BackboneConfig) -> dict[str, int]:
    """Count pending issues per entity."""
    counts: dict[str, int] = {}
    async with GitHubClient(config) as gh:
        for entity in config.entities.all_entities:
            label = f"for:{entity}"
            issues = await gh.list_open_issues(label)
            if issues:
                counts[entity] = len(issues)
    return counts


@flow(name="evening-shutdown")
async def evening_shutdown() -> dict:
    """Execute evening shutdown routine.

    1. Capture delivery statistics
    2. Count remaining pending issues
    3. List active sessions
    4. Build and return digest
    """
    config = BackboneConfig.from_toml()

    stats = await capture_delivery_stats(config)
    pending = await count_pending_issues(config)
    sessions = await list_sessions()

    notes = [
        f"Deliveries today: {stats['total']}",
        f"Failed/pending: {stats['failed']}",
    ]
    if stats["outcomes"]:
        breakdown = ", ".join(f"{k}: {v}" for k, v in stats["outcomes"].items())
        notes.append(f"Breakdown: {breakdown}")

    digest = format_digest(
        title="Evening Report",
        sessions=sessions,
        pending_counts=pending,
        notes=notes,
    )
    log.info(digest)

    return {
        "stats": stats,
        "pending": pending,
        "sessions": sessions,
        "digest": digest,
    }
