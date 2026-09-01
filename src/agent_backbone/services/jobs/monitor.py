"""The agent monitor — one tick of everything the backbone keeps an eye on.

Runs on ``timing.monitor_interval_seconds`` (60 s) and immediately at
startup. Each step is isolated: a failing step is logged and the next one
still runs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from agent_backbone.services.jobs.copy_mode import handle_copy_mode_recovery
from agent_backbone.services.jobs.escalation import (
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

log = logging.getLogger(__name__)

# Concurrency guard — prevents overlapping monitor runs
_monitor_lock = asyncio.Lock()


async def monitor_agents(
    config: BackboneConfig,
    db: BackboneDB,
    gh: object | None,
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
            return {}

        if gh is not None:
            try:
                await sync_dependencies(config, db, gh)
            except Exception:
                log.exception("Dependency sync failed (non-fatal)")

        try:
            await handle_stalls(config, active_sessions, db)
        except Exception:
            log.exception("Stall detection failed (non-fatal)")

        try:
            await handle_offline(config, active_sessions, db, gh)
        except Exception:
            log.exception("Offline detection failed (non-fatal)")

        try:
            await check_plan_waiting(config, active_sessions, db=db)
        except Exception:
            log.exception("Plan-waiting notification failed (non-fatal)")

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
        return await deliver_pending_issues(config, active_sessions, db, gh)
