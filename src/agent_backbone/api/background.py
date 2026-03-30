"""Background alignment verifier for agent state."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _poll_loop(interval: int = 30) -> None:
    """Verify DB-reported state against tmux/process observations."""
    from agent_backbone.services._locator import get_config, get_db
    from agent_backbone.services.agents import AgentState
    from agent_backbone.services.agents.interface import StateService

    while True:
        try:
            await asyncio.sleep(interval)
            config = get_config()
            db = get_db()
            state_svc = StateService(db=db)
            sessions = set(config.registry.sessions_map.values()) | set(config.registry.repo_names)
            for session in sorted(s for s in sessions if s):
                reported = await state_svc.get_reported_state(session)
                observed = await state_svc.observe_session(session)
                if not observed.online and reported.state != AgentState.OFFLINE:
                    log.warning(
                        "State alignment mismatch for %s: reported=%s observed=offline",
                        session,
                        reported.state.value,
                    )
                    continue
                if observed.online and reported.state == AgentState.OFFLINE:
                    log.warning(
                        "State alignment mismatch for %s: reported=offline observed=online",
                        session,
                    )
                    continue
                if (
                    observed.online
                    and reported.state == AgentState.IDLE
                    and observed.has_child_processes
                ):
                    log.warning(
                        "State alignment mismatch for %s: reported=idle observed=sub_agent_waiting",
                        session,
                    )
                if (
                    observed.online
                    and reported.state == AgentState.SUB_AGENT_WAITING
                    and not observed.has_child_processes
                ):
                    log.warning(
                        "State alignment mismatch for %s: reported=sub_agent_waiting observed=idle",
                        session,
                    )
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("State alignment verification failed (will retry)")


def start_background_tasks() -> None:
    """Start all background tasks. Called during gateway lifespan."""
    global _task  # noqa: PLW0603
    _task = asyncio.create_task(_poll_loop(), name="bg-state-alignment-verifier")
    log.info("Background state alignment verifier started (30s interval)")


async def stop_background_tasks() -> None:
    """Cancel all background tasks. Called during gateway shutdown."""
    global _task  # noqa: PLW0603
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    log.info("Background tasks stopped")
