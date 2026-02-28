"""Periodic agent monitor — orchestrates escalation, delivery, and sync.

Runs on a 60-second interval. Delegates to specialized submodules:
- monitoring._escalation: stall detection, offline detection, plan-waiting notification
- monitoring._pending: state-aware delivery loop
- dispatch._dependencies: sub-issue relationship sync
"""

from __future__ import annotations

import asyncio
import logging

from prefect import flow

from agent_backbone.services._locator import get_config, get_db, get_gh
from agent_backbone.services.dispatch import sync_dependencies
from agent_backbone.services.monitoring._escalation import (
    check_plan_waiting,
    handle_offline,
    handle_stalls,
)
from agent_backbone.services.monitoring._pending import deliver_pending_issues
from agent_backbone.services.tmux import list_sessions

log = logging.getLogger(__name__)

# Concurrency guard — prevents parallel monitor runs on startup burst
_monitor_lock = asyncio.Lock()


@flow(name="agent-monitor")
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

    # Sync sub-issue dependencies for open issues
    try:
        await sync_dependencies(config, db, gh)
    except Exception:
        log.exception("Dependency sync failed (non-fatal)")

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

    # Detect plan-waiting agents and send Telegram notification
    try:
        await check_plan_waiting(config, active_sessions)
    except Exception:
        log.exception("Plan-waiting notification failed (non-fatal)")

    # State-aware delivery loop
    return await deliver_pending_issues(config, active_sessions, db, gh)
