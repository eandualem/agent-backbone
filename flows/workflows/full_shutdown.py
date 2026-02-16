"""Full shutdown workflow — stop all agent sessions.

Stops every known agent session. Intended for end-of-day or maintenance
windows. Does NOT stop infrastructure (Prefect server, gateway).
"""

from __future__ import annotations

import logging

from prefect import flow, task

from src.config import BackboneConfig
from src.tmux import list_sessions, stop_session

log = logging.getLogger(__name__)


@task
async def stop_all_agents(config: BackboneConfig) -> dict[str, bool]:
    """Stop all configured agent sessions. Returns {session: success}."""
    results: dict[str, bool] = {}
    for entity, session_name in config.entities.sessions.items():
        results[session_name] = await stop_session(session_name)
    return results


@flow(name="full-shutdown")
async def full_shutdown() -> dict:
    """Shut down all agent sessions.

    Stops every configured agent session and reports results.
    Infrastructure (Prefect server, gateway, Telegram bot) is NOT affected.
    """
    config = BackboneConfig.from_toml()
    results = await stop_all_agents(config)

    stopped = [s for s, ok in results.items() if ok]
    failed = [s for s, ok in results.items() if not ok]
    remaining = await list_sessions()

    log.info(
        "Full shutdown: %d stopped, %d failed, %d still active",
        len(stopped),
        len(failed),
        len(remaining),
    )

    return {
        "stopped": stopped,
        "failed": failed,
        "remaining_sessions": remaining,
    }


if __name__ == "__main__":
    import asyncio

    asyncio.run(full_shutdown())
