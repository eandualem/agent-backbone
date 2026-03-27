"""Workflow flows: morning startup, evening shutdown, arclio start/stop, full shutdown.

Contains all Prefect @flow and @task functions for workflow templates.
"""

from __future__ import annotations

import logging

from agent_backbone.config import BackboneConfig
from agent_backbone.services._locator import get_config, get_db, get_gh
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.messaging import deliver_message
from agent_backbone.services.terminal import (
    RUNTIME_ENV_KEY,
    list_sessions,
    resolve_agent_dir,
    start_session,
    stop_session,
)

log = logging.getLogger(__name__)

_DEFAULT_AGENT_CLI = "claude"


def _format_digest(
    title: str,
    sessions: list[str],
    pending_counts: dict[str, int],
    notes: list[str] | None = None,
) -> str:
    """Format a simple digest message for logging."""
    lines = [f"[via:backbone] === {title} ==="]
    lines.append(f"Active sessions: {len(sessions)}")
    if sessions:
        lines.append("  " + ", ".join(sorted(sessions)))
    if pending_counts:
        lines.append("Pending issues:")
        for entity, count in sorted(pending_counts.items()):
            lines.append(f"  {entity}: {count}")
    else:
        lines.append("No pending issues.")
    if notes:
        lines.append("")
        for note in notes:
            lines.append(f"  {note}")
    return "\n".join(lines)


async def _start_workflow_agent_session(session_name: str, config: BackboneConfig) -> bool:
    """Start a workflow-managed agent session with its resolved workspace."""
    working_dir = resolve_agent_dir(session_name, config.registry)
    if not working_dir:
        log.warning("Workflow start skipped for %s: no working directory resolved", session_name)
        return False

    return await start_session(
        session_name,
        working_dir=working_dir,
        command=[_DEFAULT_AGENT_CLI],
        environment={RUNTIME_ENV_KEY: _DEFAULT_AGENT_CLI},
    )


# ---------------------------------------------------------------------------
# Morning startup
# ---------------------------------------------------------------------------


async def count_pending_issues(
    config: BackboneConfig,
    gh: object,
) -> dict[str, int]:
    """Count pending issues per entity."""
    counts: dict[str, int] = {}
    for entity, entry in config.registry.entities.items():
        if entry.entity_type == "role" or entry.session is None:
            continue
        label = f"for:{entity}"
        issues = await gh.list_open_issues(label)
        if issues:
            counts[entity] = len(issues)
    return counts


async def deliver_overnight_issues(
    config: BackboneConfig,
    pending: dict[str, int],
    gh: object,
) -> dict[str, str]:
    """Deliver the highest-priority pending issue to each online agent."""
    results: dict[str, str] = {}
    sessions = set(await list_sessions())

    for entity in config.registry.all_entities:
        session = config.registry.sessions_map.get(entity)
        if not session or session not in sessions:
            continue
        if entity not in pending:
            continue

        label = f"for:{entity}"
        issues = await gh.list_open_issues(label)
        if issues:
            issue = issues[0]
            msg = (
                f"[via:backbone] Next issue in your queue: "
                f'#{issue.number} "{issue.title}" '
                f"(from {issue.labels.sender})"
            )
            outcome = await deliver_message(session, msg, config)
            if outcome == "delivered":
                results[entity] = f"delivered_#{issues[0].number}"
            else:
                results[entity] = outcome

    return results


async def morning_startup() -> dict:
    """Execute morning startup routine.

    1. Count pending issues per entity
    2. Deliver overnight issues to online agents
    3. Return summary for Telegram digest
    """
    config = get_config()
    gh = get_gh()

    pending = await count_pending_issues(config, gh)
    deliveries = await deliver_overnight_issues(config, pending, gh)
    sessions = await list_sessions()

    digest = _format_digest(
        title="Morning Startup",
        sessions=sessions,
        pending_counts=pending,
    )
    log.info(digest)

    return {
        "started": [],
        "pending": pending,
        "deliveries": deliveries,
        "digest": digest,
    }


# ---------------------------------------------------------------------------
# Evening shutdown
# ---------------------------------------------------------------------------


async def capture_delivery_stats(config: BackboneConfig, db: BackboneDB) -> dict:
    """Get delivery statistics for the day."""
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


async def _count_pending_evening(config: BackboneConfig, gh: object) -> dict[str, int]:
    """Count pending issues per entity (evening variant)."""
    counts: dict[str, int] = {}
    for entity, entry in config.registry.entities.items():
        if entry.entity_type == "role" or entry.session is None:
            continue
        label = f"for:{entity}"
        issues = await gh.list_open_issues(label)
        if issues:
            counts[entity] = len(issues)
    return counts


async def evening_shutdown() -> dict:
    """Execute evening shutdown routine.

    1. Capture delivery statistics
    2. Count remaining pending issues
    3. List active sessions
    4. Build and return digest
    """
    config = get_config()
    db = get_db()
    gh = get_gh()

    stats = await capture_delivery_stats(config, db)
    pending = await _count_pending_evening(config, gh)
    sessions = await list_sessions()

    notes = [
        f"Deliveries today: {stats['total']}",
        f"Failed/pending: {stats['failed']}",
    ]
    if stats["outcomes"]:
        breakdown = ", ".join(f"{k}: {v}" for k, v in stats["outcomes"].items())
        notes.append(f"Breakdown: {breakdown}")

    digest = _format_digest(
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


# ---------------------------------------------------------------------------
# Arclio start
# ---------------------------------------------------------------------------

# Arclio-specific agents to start
ARCLIO_AGENTS = ["ike", "feynman"]


async def start_arclio_agents(config: BackboneConfig) -> list[str]:
    """Start Arclio-related agent sessions."""
    started = []
    for agent in ARCLIO_AGENTS:
        session_name = config.registry.sessions_map.get(agent, agent)
        if await _start_workflow_agent_session(session_name, config):
            started.append(session_name)
    return started


async def arclio_start() -> dict:
    """Start Arclio development environment.

    Starts Arclio coding agents and reports which sessions came online.
    """
    config = get_config()
    started = await start_arclio_agents(config)
    sessions = await list_sessions()

    log.info("Arclio start complete: %d agents started", len(started))
    return {
        "started": started,
        "active_sessions": sessions,
    }


# ---------------------------------------------------------------------------
# Arclio stop
# ---------------------------------------------------------------------------

# Arclio-specific agents to stop
ARCLIO_AGENTS_STOP = ["ike", "feynman"]


async def stop_arclio_agents(config: BackboneConfig) -> list[str]:
    """Stop Arclio-related agent sessions."""
    stopped = []
    for agent in ARCLIO_AGENTS_STOP:
        session_name = config.registry.sessions_map.get(agent, agent)
        if await stop_session(session_name):
            stopped.append(session_name)
    return stopped


async def arclio_stop() -> dict:
    """Shut down Arclio development environment.

    Stops all Arclio coding agents and reports final session state.
    """
    config = get_config()
    stopped = await stop_arclio_agents(config)
    sessions = await list_sessions()

    log.info("Arclio stop complete: %d agents stopped", len(stopped))
    return {
        "stopped": stopped,
        "remaining_sessions": sessions,
    }


# ---------------------------------------------------------------------------
# Full shutdown
# ---------------------------------------------------------------------------


async def stop_all_agents(config: BackboneConfig) -> dict[str, bool]:
    """Stop all configured agent sessions. Returns {session: success}."""
    results: dict[str, bool] = {}
    for entity, session_name in config.registry.sessions_map.items():
        results[session_name] = await stop_session(session_name)
    return results


async def full_shutdown() -> dict:
    """Shut down all agent sessions.

    Stops every configured agent session and reports results.
    Infrastructure (Prefect server, gateway, Telegram bot) is NOT affected.
    """
    config = get_config()
    results = await stop_all_agents(config)

    stopped = [s for s, ok in results.items() if ok]
    failed = [s for s, ok in results.items() if not ok]
    remaining = await list_sessions()

    log.info(
        "Full shutdown: %d stopped, %d failed, %d still active",
        len(stopped),
        len(failed),
        len(remaining),
    )

    return {
        "stopped": stopped,
        "failed": failed,
        "remaining_sessions": remaining,
    }
