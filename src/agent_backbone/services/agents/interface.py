"""Agent state tracking service — the API's handle on ``get_agent_state``."""

from __future__ import annotations

import logging
from pathlib import Path

from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents._inference import get_agent_state
from agent_backbone.services.agents.models import StateSnapshot

log = logging.getLogger(__name__)


class StateService:
    """Lifecycle component owning the state directory.

    Every state decision goes through ``get_agent_state``; the database
    mirror of these snapshots (used by the dead-session check and the
    digest) is kept by the monitor job, not on the read path.
    """

    def __init__(self, state_dir: str | Path, stale_threshold: int = 300) -> None:
        self._state_dir = Path(state_dir).expanduser()
        self._stale_threshold = stale_threshold

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    async def start(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        log.info("State service started: state_dir=%s", self._state_dir)

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {
            "healthy": self._state_dir.is_dir(),
            "service": "state",
            "state_dir": str(self._state_dir),
        }

    async def get_state(self, session: str) -> StateSnapshot:
        """Reconciled agent state: fresh hook state first, the terminal as fallback."""
        return await get_agent_state(self._state_dir, session, self._stale_threshold)

    def read_state(self, session: str) -> StateSnapshot | None:
        """The hook-written state file alone (no terminal read)."""
        return read_state_file(self._state_dir, session)
