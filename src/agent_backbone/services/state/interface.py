"""Agent state tracking service — LifecycleAware wrapper."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class StateService:
    """Agent state tracking service implementing LifecycleAware."""

    def __init__(self, state_dir: str = "~/.claude/state", stale_threshold: int = 300) -> None:
        self._state_dir = Path(state_dir).expanduser()
        self._stale_threshold = stale_threshold

    async def start(self) -> None:
        log.info("State service started: state_dir=%s", self._state_dir)

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {
            "healthy": self._state_dir.is_dir(),
            "service": "state",
            "state_dir_exists": self._state_dir.is_dir(),
        }
