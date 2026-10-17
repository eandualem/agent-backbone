"""The agent monitor — one tick of everything the backbone keeps an eye on.

Runs on ``timing.monitor_interval_seconds`` (60 s) and immediately at
startup. Each agent's state is read once per tick and handed to every
step; each step is isolated — a failing step is logged and the next one
still runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from agent_backbone.services.agents import StateSnapshot, agent_state, collect_usage
from agent_backbone.services.github import QueueSnapshot
from agent_backbone.services.jobs.context_note import note_context_size
from agent_backbone.services.jobs.diagnostics import observe_job, observe_runtime
from agent_backbone.services.jobs.escalation import (
    check_blocked,
    check_permission_denials,
    check_permission_waiting,
    check_plan_waiting,
    handle_offline,
    handle_stalls,
)
from agent_backbone.services.jobs.pending import deliver_pending_issues
from agent_backbone.services.jobs.retry import drain_message_queue
from agent_backbone.services.routing import sync_dependencies
from agent_backbone.services.terminal import capture_pane, list_sessions

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.github import GitHubClient

log = logging.getLogger(__name__)

# Concurrency guard — prevents overlapping monitor runs
_monitor_lock = asyncio.Lock()

_last_steps: dict[str, int] = {}
"""Milliseconds each step of the latest pass took, for its slow-run record."""

DEPENDENCY_SYNC_INTERVAL_SECONDS = 600
"""Sub-issue links change rarely, and a close asks GitHub for its own parent."""

_dependency_synced_at: float | None = None


def _dependency_sync_due() -> bool:
    """True at most once every ``DEPENDENCY_SYNC_INTERVAL_SECONDS``."""
    global _dependency_synced_at
    now = time.monotonic()
    if (
        _dependency_synced_at is not None
        and now - _dependency_synced_at < DEPENDENCY_SYNC_INTERVAL_SECONDS
    ):
        return False
    _dependency_synced_at = now
    return True


def take_pass_steps() -> dict[str, int]:
    """Each step of the latest monitor pass, in order, with its milliseconds.

    Taking them clears them, so a run that never reached a pass (a failed
    refresh) does not pick up the previous pass's steps."""
    steps = dict(_last_steps)
    _last_steps.clear()
    return steps


def _step_clock() -> Callable[[str], None]:
    """Note in ``_last_steps`` the milliseconds since the previous step ended."""
    mark = time.monotonic()

    def lap(step: str) -> None:
        nonlocal mark
        now = time.monotonic()
        _last_steps[step] = int((now - mark) * 1000)
        mark = now

    return lap


AgentStates = dict[str, StateSnapshot]
"""State of every configured agent with a live session, read once per tick."""


async def read_states(config: BackboneConfig, active_sessions: set[str]) -> AgentStates:
    """One reconciled snapshot per configured agent whose session is up."""
    states: AgentStates = {}
    for spec in config.agents:
        # An inbox-only agent has no session: one with its name is not its own.
        if spec.name in active_sessions and not spec.inbox_only:
            pane = await capture_pane(spec.name)
            states[spec.name] = await agent_state(config, spec.name, pane_content=pane)
    return states


async def sync_states(db: BackboneDB, states: AgentStates) -> None:
    """Mirror the snapshots into ``agent_states`` (read by the offline check and status)."""
    for name, snapshot in states.items():
        try:
            await db.states.set(session_name=name, **snapshot.db_fields())
        except Exception as exc:
            log.exception("Failed to persist agent state for %s (non-fatal)", name)
            await observe_job(
                db,
                source="agent-monitor",
                stage="state_persistence",
                agent_name=name,
                error_type=type(exc).__name__,
            )
        else:
            await observe_job(
                db, source="agent-monitor", stage="state_persistence", agent_name=name
            )


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
        _last_steps.clear()
        lap = _step_clock()
        if gh is not None:
            gh = QueueSnapshot(gh)
        active_sessions = set(await list_sessions())
        if not active_sessions:
            log.debug("No tmux sessions active")

        states = await read_states(config, active_sessions)
        await observe_runtime(db, config, states)
        await sync_states(db, states)
        lap("state_read")
        if gh is not None and _dependency_sync_due():
            try:
                await sync_dependencies(config, db, gh)
            except Exception as exc:
                log.exception("Dependency sync failed (non-fatal)")
                await observe_job(
                    db,
                    source="agent-monitor",
                    stage="dependency_sync",
                    error_type=type(exc).__name__,
                )
            else:
                await observe_job(db, source="agent-monitor", stage="dependency_sync")
            lap("dependency_sync")

        try:
            await handle_stalls(config, states, db)
        except Exception as exc:
            log.exception("Stall detection failed (non-fatal)")
            await observe_job(
                db,
                source="agent-monitor",
                stage="stall_detection",
                error_type=type(exc).__name__,
            )
        else:
            await observe_job(db, source="agent-monitor", stage="stall_detection")
        lap("stall_detection")

        try:
            await handle_offline(config, active_sessions, db, gh)
        except Exception as exc:
            log.exception("Offline detection failed (non-fatal)")
            await observe_job(
                db,
                source="agent-monitor",
                stage="offline_detection",
                error_type=type(exc).__name__,
            )
        else:
            await observe_job(db, source="agent-monitor", stage="offline_detection")
        lap("offline_detection")

        try:
            await check_plan_waiting(config, states, db=db)
        except Exception as exc:
            log.exception("Plan-waiting notification failed (non-fatal)")
            await observe_job(
                db,
                source="agent-monitor",
                stage="plan_notification",
                error_type=type(exc).__name__,
            )
        else:
            await observe_job(db, source="agent-monitor", stage="plan_notification")
        lap("plan_notification")

        try:
            await check_blocked(config, states, db=db)
        except Exception as exc:
            log.exception("Blocked-agent notification failed (non-fatal)")
            await observe_job(
                db,
                source="agent-monitor",
                stage="blocked_notification",
                error_type=type(exc).__name__,
            )
        else:
            await observe_job(db, source="agent-monitor", stage="blocked_notification")
        lap("blocked_notification")

        try:
            await check_permission_denials(config)
        except Exception as exc:
            log.exception("Permission-denied notification failed (non-fatal)")
            await observe_job(
                db,
                source="agent-monitor",
                stage="denial_notification",
                error_type=type(exc).__name__,
            )
        else:
            await observe_job(db, source="agent-monitor", stage="denial_notification")
        lap("denial_notification")

        try:
            await check_permission_waiting(config, states)
        except Exception as exc:
            log.exception("Permission-waiting notification failed (non-fatal)")
            await observe_job(
                db,
                source="agent-monitor",
                stage="permission_notification",
                error_type=type(exc).__name__,
            )
        else:
            await observe_job(db, source="agent-monitor", stage="permission_notification")
        lap("permission_notification")

        if on_change is not None:
            try:
                await on_change()
            except Exception as exc:
                log.exception("Session snapshot broadcast failed (non-fatal)")
                await observe_job(
                    db,
                    source="agent-monitor",
                    stage="snapshot_broadcast",
                    error_type=type(exc).__name__,
                )
            else:
                await observe_job(db, source="agent-monitor", stage="snapshot_broadcast")
            lap("snapshot_broadcast")

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
        except Exception as exc:
            log.exception("Queue drain during monitor failed (non-fatal)")
            await observe_job(
                db,
                source="agent-monitor",
                stage="queue_drain",
                error_type=type(exc).__name__,
            )
        else:
            await observe_job(db, source="agent-monitor", stage="queue_drain")
        lap("queue_drain")

        # Usage is read after the drain: it can take a second, and queued work
        # waits on nothing it measures. The context note reads what it records.
        try:
            usage_result = await collect_usage(config, db)
        except Exception as exc:
            log.exception("Usage collection failed (non-fatal)")
            await observe_job(
                db, source="agent-monitor", stage="usage_collection", error_type=type(exc).__name__
            )
        else:
            for check in usage_result.get("checks", []):
                await observe_job(db, source="agent-monitor", **check)
            await observe_job(
                db,
                source="agent-monitor",
                stage="usage_collection",
                error_type="usage_collection_errors" if usage_result.get("errors") else None,
            )
        lap("usage_collection")

        try:
            await note_context_size(config, db, states)
        except Exception as exc:
            log.exception("Context-size note failed (non-fatal)")
            await observe_job(
                db, source="agent-monitor", stage="context_note", error_type=type(exc).__name__
            )
        else:
            await observe_job(db, source="agent-monitor", stage="context_note")
        lap("context_note")

        if gh is None:
            return {}
        result = await deliver_pending_issues(config, states, db, gh)
        lap("pending_issues")
        return result
