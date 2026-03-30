"""Startup seeding for the DB-backed agent state pipeline."""

from __future__ import annotations

import logging

from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents._observation import observe_session, snapshot_from_observation
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.terminal import list_sessions

log = logging.getLogger(__name__)


async def seed_startup_states(config: BackboneConfig, db: BackboneDB) -> None:
    """Populate PostgreSQL from live tmux/process observations on startup."""
    try:
        active_sessions = set(await list_sessions())
    except Exception:
        log.exception("Startup state seeding: list_sessions failed (non-fatal)")
        return

    if not active_sessions:
        log.info("Startup state seeding: no active sessions")
        return

    repo_names_lower = {name.lower() for name in config.registry.repo_names}
    seeded = 0
    for session_name in sorted(active_sessions):
        if session_name in config.entities.service_sessions:
            continue
        if (
            session_name not in config.registry.entity_by_session
            and session_name.lower() not in repo_names_lower
        ):
            continue

        try:
            observation = await observe_session(session_name)
            snapshot = snapshot_from_observation(observation)
            await db.set_agent_state(
                session_name=session_name,
                state=snapshot.state.value,
                current_issue=None,
                entity=config.registry.entity_by_session.get(session_name, session_name),
                context=None,
                ts=str(snapshot.timestamp),
                plan_file=None,
                plan_title=None,
            )
            seeded += 1
        except Exception:
            log.exception("Startup state seeding failed for %s (non-fatal)", session_name)

    log.info("Startup state seeding complete — %d session(s) initialized", seeded)
