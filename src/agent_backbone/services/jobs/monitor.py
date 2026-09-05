"""The agent monitor — one tick of everything the backbone keeps an eye on.

Runs on ``timing.monitor_interval_seconds`` (60 s) and immediately at
startup. Each agent's state is read once per tick and handed to every
step; each step is isolated — a failing step is logged and the next one
still runs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from agent_backbone.services.agents import StateSnapshot, agent_state
from agent_backbone.services.jobs.copy_mode import handle_copy_mode_recovery
from agent_backbone.services.jobs.escalation import (
    check_blocked,
    check_permission_waiting,
    check_plan_waiting,
    handle_offline,
    handle_stalls,
)
from agent_backbone.services.jobs.pending import deliver_pending_issues
from agent_backbone.services.jobs.retry import drain_message_queue
from agent_backbone.services.routing import sync_dependencies
from agent_backbone.services.terminal import list_sessions

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)

# Concurrency guard — prevents overlapping monitor runs
_monitor_lock = asyncio.Lock()

AgentStates = dict[str, StateSnapshot]
"""State of every configured agent with a live session, read once per tick."""


async def read_states(config: BackboneConfig, active_sessions: set[str]) -> AgentStates:
    """One reconciled snapshot per configured agent whose session is up."""
    states: AgentStates = {}
    for name in config.agents.names:
        if name in active_sessions:
            states[name] = await agent_state(config, name)
    return states


async def sync_states(db: BackboneDB, states: AgentStates) -> None:
    """Mirror the snapshots into ``agent_states`` (read by the offline check and status)."""
    for name, snapshot in states.items():
        try:
            await db.states.set(session_name=name, **snapshot.db_fields())
        except Exception:
            log.exception("Failed to persist agent state for %s (non-fatal)", name)


async def monitor_agents(
    config: BackboneConfig,
    db: BackboneDB,
    gh: GitHubClient | None,
    *,
    on_change: Callable[[], Awaitable[object]] | None = None,
) -> dict:
    """Check all configured agents; escalate problems and deliver pending issues.

    ``on_change`` is awaited once per tick so the API can push a fresh
    session snapshot to its subscribers. Returns a dict mapping agent →
    action taken.
    """
    if _monitor_lock.locked():
        log.info("Monitor already running — skipping concurrent run")
        return {"_skipped": "concurrent_run"}

    async with _monitor_lock:
        active_sessions = set(await list_sessions())
        if not active_sessions:
            log.debug("No tmux sessions active")

        states = await read_states(config, active_sessions)
        await sync_states(db, states)

        if gh is not None:
            try:
                await sync_dependencies(config, db, gh)
            except Exception:
                log.exception("Dependency sync failed (non-fatal)")

        try:
            await handle_stalls(config, states, db)
        except Exception:
            log.exception("Stall detection failed (non-fatal)")

        try:
            await handle_offline(config, active_sessions, db, gh)
        except Exception:
            log.exception("Offline detection failed (non-fatal)")

        try:
            await check_plan_waiting(config, states, db=db)
        except Exception:
            log.exception("Plan-waiting notification failed (non-fatal)")

        try:
            await check_blocked(config, states, db=db)
        except Exception:
            log.exception("Blocked-agent notification failed (non-fatal)")

        try:
            await check_permission_waiting(config, states)
        except Exception:
            log.exception("Permission-waiting notification failed (non-fatal)")

        try:
            await handle_copy_mode_recovery(config, active_sessions)
        except Exception:
            log.exception("Copy-mode recovery failed (non-fatal)")

        if on_change is not None:
            try:
                await on_change()
            except Exception:
                log.exception("Session snapshot broadcast failed (non-fatal)")

        # Drain deferred comments/messages for sessions that are now idle.
        try:
            queue_summary = await drain_message_queue(
                config=config,
                db=db,
                gh=gh,
                active_sessions=active_sessions,
            )
            if queue_summary:
                log.info("Monitor queue drain: %s", queue_summary)
        except Exception:
            log.exception("Queue drain during monitor failed (non-fatal)")

        if gh is None:
            return {}
        return await deliver_pending_issues(config, states, db, gh)
