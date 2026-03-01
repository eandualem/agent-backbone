"""Tmux service — async tmux operations."""

import logging
import shutil

from agent_backbone.services.tmux._core import (
    capture_pane as _capture_pane,
)
from agent_backbone.services.tmux._core import (
    send_keys as _send_keys,
)
from agent_backbone.services.tmux._core import (
    session_exists as _session_exists,
)
from agent_backbone.services.tmux._sessions import (
    list_sessions as _list_sessions,
)
from agent_backbone.services.tmux._sessions import (
    list_sessions_rich as _list_sessions_rich,
)

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

    # --- DI surface for route handlers ---

    async def list_sessions(self) -> list[str]:
        """List active tmux session names."""
        return await _list_sessions()

    async def list_sessions_rich(self) -> list[dict]:
        """List sessions with metadata (windows, created, attached)."""
        return await _list_sessions_rich()

    async def capture_pane(self, session: str, lines: int = 50) -> str:
        """Capture recent terminal output from a session."""
        return await _capture_pane(session, lines=lines)

    async def session_exists(self, session: str) -> bool:
        """Check if a tmux session exists."""
        return await _session_exists(session)

    async def send_keys(self, session: str, keys: str, literal: bool = True) -> bool:
        """Send keys to a tmux session."""
        return await _send_keys(session, keys, literal=literal)
