"""Tmux service — async tmux operations."""

import logging
import shutil

log = logging.getLogger(__name__)


class TmuxService:
    """Tmux operations service implementing LifecycleAware."""

    async def start(self) -> None:
        log.info("Tmux service started")

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        tmux_available = shutil.which("tmux") is not None
        return {
            "healthy": tmux_available,
            "service": "tmux",
            "tmux_available": tmux_available,
        }
