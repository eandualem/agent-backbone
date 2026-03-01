"""Agent state tracking service — LifecycleAware wrapper."""

from __future__ import annotations

import logging
from pathlib import Path

from agent_backbone.services.state._file_reader import read_state_file
from agent_backbone.services.state._inference import get_agent_state as _get_agent_state
from agent_backbone.services.state.models import StateSnapshot

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

    # --- DI surface for route handlers ---

    async def get_state(self, session: str) -> StateSnapshot:
        """Get reconciled agent state (push+pull) for a session."""
        return await _get_agent_state(self._state_dir, session, self._stale_threshold)

    def read_state(self, session: str) -> StateSnapshot | None:
        """Read push-based state file for a session."""
        return read_state_file(self._state_dir, session)
