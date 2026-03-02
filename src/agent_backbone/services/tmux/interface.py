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
from agent_backbone.services.tmux._sessions import (
    start_session as _start_session,
)
from agent_backbone.services.tmux._sessions import (
    stop_session as _stop_session,
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

    async def start_session(
        self,
        session_name: str,
        working_dir: str | None = None,
        command: list[str] | None = None,
        environment: dict[str, str] | None = None,
    ) -> bool:
        """Start a new detached tmux session."""
        return await _start_session(
            session_name,
            working_dir=working_dir,
            command=command,
            environment=environment,
        )

    async def stop_session(self, session_name: str) -> bool:
        """Stop (kill) a tmux session."""
        return await _stop_session(session_name)
