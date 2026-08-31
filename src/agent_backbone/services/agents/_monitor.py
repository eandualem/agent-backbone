"""Periodic agent monitor — orchestrates escalation, delivery, and sync.

Runs on the scheduler interval (default 60s). Delegates to:
- agents._escalation: stall detection, offline detection, plan-waiting notification
- agents._pending: state-aware delivery loop
- routing._dependencies: sub-issue relationship sync
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from agent_backbone.api.session_updates import emit_sessions_update
from agent_backbone.services.agents._escalation import (
    check_plan_waiting,
    handle_offline,
    handle_stalls,
)
from agent_backbone.services.agents._pending import deliver_pending_issues
from agent_backbone.services.routing._dependencies import sync_dependencies
from agent_backbone.services.routing._flows import drain_message_queue
from agent_backbone.services.terminal import handle_copy_mode_recovery, list_sessions

if TYPE_CHECKING:
    import socketio

    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.agents.interface import StateService
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.terminal import TmuxService

log = logging.getLogger(__name__)

# Concurrency guard — prevents overlapping monitor runs
_monitor_lock = asyncio.Lock()


async def monitor_agents(
    config: BackboneConfig,
    db: BackboneDB,
    gh: object | None,
    *,
    state_svc: StateService | None = None,
    tmux_svc: TmuxService | None = None,
    sio: socketio.AsyncServer | None = None,
) -> dict:
    """Check all configured agents; escalate problems and deliver pending issues.

    Returns a dict mapping agent → action taken.
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

        # Reconcile out-of-band session churn to live Socket.IO subscribers.
        try:
            await emit_sessions_update(sio, config, state_svc, tmux_svc, only_if_changed=True)
        except Exception:
            log.exception("Session subscription reconciliation failed (non-fatal)")

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
