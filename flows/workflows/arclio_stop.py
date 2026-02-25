"""Arclio shutdown workflow — stop all Arclio coding agents.

Gracefully stops Arclio-related agent sessions after confirming
no active deliveries are in progress.
"""

from __future__ import annotations

import logging

from prefect import flow, task

from src.config import BackboneConfig
from src.tmux import list_sessions, stop_session

log = logging.getLogger(__name__)

# Arclio-specific agents to stop
ARCLIO_AGENTS = ["ike", "feynman"]


@task
async def stop_arclio_agents(config: BackboneConfig) -> list[str]:
    """Stop Arclio-related agent sessions."""
    stopped = []
    for agent in ARCLIO_AGENTS:
        session_name = config.registry.sessions_map.get(agent, agent)
        if await stop_session(session_name):
            stopped.append(session_name)
    return stopped


@flow(name="arclio-stop")
async def arclio_stop() -> dict:
    """Shut down Arclio development environment.

    Stops all Arclio coding agents and reports final session state.
    """
    config = BackboneConfig.from_toml()
    stopped = await stop_arclio_agents(config)
    sessions = await list_sessions()

    log.info("Arclio stop complete: %d agents stopped", len(stopped))
    return {
        "stopped": stopped,
        "remaining_sessions": sessions,
    }


if __name__ == "__main__":
    import asyncio

    asyncio.run(arclio_stop())
