"""Arclio startup workflow — start all coding agents for Arclio repos.

Starts configured Arclio-related agent sessions and delivers any pending
issues to them once they're online.
"""

from __future__ import annotations

import logging

from prefect import flow, task

from src.config import BackboneConfig
from src.tmux import list_sessions, start_session

log = logging.getLogger(__name__)

# Arclio-specific agents to start
ARCLIO_AGENTS = ["ike", "feynman"]


@task
async def start_arclio_agents(config: BackboneConfig) -> list[str]:
    """Start Arclio-related agent sessions."""
    started = []
    for agent in ARCLIO_AGENTS:
        session_name = config.registry.sessions_map.get(agent, agent)
        if await start_session(session_name):
            started.append(session_name)
    return started


@flow(name="arclio-start")
async def arclio_start() -> dict:
    """Start Arclio development environment.

    Starts Arclio coding agents and reports which sessions came online.
    """
    config = BackboneConfig.from_toml()
    started = await start_arclio_agents(config)
    sessions = await list_sessions()

    log.info("Arclio start complete: %d agents started", len(started))
    return {
        "started": started,
        "active_sessions": sessions,
    }


if __name__ == "__main__":
    import asyncio

    asyncio.run(arclio_start())
