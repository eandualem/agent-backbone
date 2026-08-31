"""Startup state reconciliation — syncs disk state to DB on backbone restart."""

from __future__ import annotations

import logging

from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents._escalation import check_plan_waiting
from agent_backbone.services.agents._inference import get_agent_state
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.terminal import list_sessions

log = logging.getLogger(__name__)


async def reconcile_startup_states(config: BackboneConfig, db: BackboneDB) -> None:
    """Sync agent states to DB and fire plan-waiting notifications on startup.

    All errors are non-fatal — a reconciliation failure must not block startup.
    """
    try:
        active_sessions = set(await list_sessions())
    except Exception:
        log.exception("Startup reconciliation: list_sessions failed (non-fatal)")
        return

    if not active_sessions:
        log.info("Startup reconciliation: no active sessions")
        return

    state_path = config.state_dir
    stale_threshold = config.agent_state.stale_threshold_seconds

    for name in config.agents.names:
        if name not in active_sessions:
            continue
        try:
            snapshot = await get_agent_state(state_path, name, stale_threshold)
            await db.set_agent_state(
                session_name=name,
                state=snapshot.state.value,
                current_issue=snapshot.current_issue,
                entity=name,
                plan_file=snapshot.plan_file,
                plan_title=snapshot.plan_title,
                reason=snapshot.reason,
                current_repo=snapshot.current_repo,
            )
        except Exception:
            log.exception("Startup reconciliation: failed to sync state for %s", name)

    try:
        await check_plan_waiting(config, active_sessions, db=db)
    except Exception:
        log.exception("Startup reconciliation: plan-waiting check failed (non-fatal)")

    log.info("Startup reconciliation complete — %d active sessions", len(active_sessions))
