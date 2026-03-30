"""Periodic agent monitor — orchestrates escalation, delivery, and sync.

Runs on a 60-second interval. Delegates to specialized submodules:
- agents._escalation: stall detection, offline detection, plan-waiting notification
- agents._pending: state-aware delivery loop
- routing._dependencies: sub-issue relationship sync
"""

from __future__ import annotations

import asyncio
import logging

from agent_backbone.api.session_updates import emit_sessions_update
from agent_backbone.services._locator import get_config, get_db, get_gh, get_sio
from agent_backbone.services.agents._escalation import (
    check_plan_waiting,
    handle_offline,
    handle_stalls,
)
from agent_backbone.services.agents._pending import deliver_pending_issues
from agent_backbone.services.agents.interface import StateService
from agent_backbone.services.telemetry import collect_active_session_telemetry
from agent_backbone.services.terminal import TmuxService, handle_copy_mode_recovery, list_sessions

log = logging.getLogger(__name__)

# Concurrency guard — prevents parallel monitor runs on startup burst
_monitor_lock = asyncio.Lock()


async def monitor_agents() -> dict:
    """Check all entity sessions and deliver pending issues to online agents.

    Also detects stalled and unexpectedly offline agents, escalating to
    the configured target with dedup.

    Returns a dict mapping entity → action taken.
    """
    # Prevent concurrent runs (Prefect may schedule a burst on startup)
    if _monitor_lock.locked():
        log.info("Monitor already running — skipping concurrent run")
        return {"_skipped": "concurrent_run"}

    async with _monitor_lock:
        return await _monitor_agents_impl()


async def _monitor_agents_impl() -> dict:
    """Inner implementation of monitor_agents, guarded by _monitor_lock."""
    config = get_config()
    db = get_db()
    gh = get_gh()

    active_sessions = set(await list_sessions())
    if not active_sessions:
        log.info("No tmux sessions active")
        return {}

    # Detect stalled agents
    try:
        await handle_stalls(config, active_sessions, db)
    except Exception:
        log.exception("Stall detection failed (non-fatal)")

    # Detect unexpectedly offline agents
    try:
        await handle_offline(config, active_sessions, db, gh)
    except Exception:
        log.exception("Offline detection failed (non-fatal)")

    # Mark swarm workers failed if their tmux session vanished mid-run.
    try:
        fresh_active_sessions = set(await list_sessions())
        lost_workers = await db.reconcile_swarm_worker_sessions(fresh_active_sessions)
        if lost_workers:
            log.warning("Marked %d swarm worker session(s) lost", lost_workers)
    except Exception:
        log.exception("Swarm worker session reconciliation failed (non-fatal)")

    # Detect plan-waiting agents and send Telegram notification
    try:
        await check_plan_waiting(config, active_sessions, db=db)
    except Exception:
        log.exception("Plan-waiting notification failed (non-fatal)")

    # Detect copy mode, attempt auto-recovery, and escalate stuck incidents.
    try:
        await handle_copy_mode_recovery(config, active_sessions)
    except Exception:
        log.exception("Copy-mode recovery failed (non-fatal)")

    # Collect runtime-native telemetry into agent_activity.
    try:
        await collect_active_session_telemetry(
            config=config,
            db=db,
            active_sessions=active_sessions,
        )
    except Exception:
        log.exception("Telemetry collection failed (non-fatal)")

    # Reconcile out-of-band session churn to live Socket.IO subscribers.
    try:
        await emit_sessions_update(
            get_sio(),
            config,
            StateService(db=db),
            TmuxService(),
            only_if_changed=True,
        )
    except Exception:
        log.exception("Session subscription reconciliation failed (non-fatal)")

    # State-aware delivery loop
    return await deliver_pending_issues(config, active_sessions, db, gh)
