"""Background state polling — emits session updates via Socket.IO."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _poll_loop(interval: int = 30) -> None:
    """Poll agent state and emit sessions:update on a fixed interval."""
    from agent_backbone.api.session_updates import emit_sessions_update
    from agent_backbone.services._locator import get_config, get_sio
    from agent_backbone.services.agents.interface import StateService
    from agent_backbone.services.terminal import TmuxService

    while True:
        try:
            await asyncio.sleep(interval)
            config = get_config()
            sio = get_sio()
            if sio is None:
                continue
            db = None
            try:
                from agent_backbone.services._locator import get_db
                db = get_db()
            except RuntimeError:
                pass
            state_svc = StateService(
                state_dir=config.agent_state.state_dir,
                stale_threshold=config.agent_state.stale_threshold_seconds,
                db=db,
            )
            tmux_svc = TmuxService()
            await emit_sessions_update(sio, config, state_svc, tmux_svc, only_if_changed=True)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("State poll failed (will retry)")


def start_background_tasks() -> None:
    """Start all background tasks. Called during gateway lifespan."""
    global _task  # noqa: PLW0603
    _task = asyncio.create_task(_poll_loop(), name="bg-state-poller")
    log.info("Background state poller started (30s interval)")


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
