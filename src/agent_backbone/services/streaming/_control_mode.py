"""Tmux control mode streaming.

Provides real-time character-by-character output from tmux sessions via
control mode (`tmux -C attach-session -t {name} -r`). Long-lived subprocess
connections with automatic reconnection on unexpected disconnects.
"""

from __future__ import annotations

import asyncio
import logging
import re
import signal
from collections.abc import AsyncIterator

from agent_backbone.config import ControlModeConfig
from agent_backbone.services.streaming.models import (
    ConnectionState,
    ControlModeEvent,
    ExitEvent,
    LayoutChangeEvent,
    ModeChangeEvent,
    OutputEvent,
    SessionEvent,
    WindowEvent,
)

log = logging.getLogger(__name__)

# Regex for tmux octal escapes: \ddd where d is 0-7
_OCTAL_RE = re.compile(r"\\(\d{3})")


def _unescape_output(raw: str) -> str:
    """Unescape tmux control mode octal sequences.

    Tmux encodes non-printable bytes as \\ddd (3-digit octal).
    Python's unicode_escape codec mishandles bytes >127, so we use
    regex replacement instead.
    """

    def _replace(m: re.Match) -> str:
        return chr(int(m.group(1), 8))

    return _OCTAL_RE.sub(_replace, raw)


def parse_control_mode_line(line: str) -> ControlModeEvent | None:
    """Parse a single tmux control mode line into an event.

    Returns None for unrecognized lines, %begin/%end/%error markers,
    and empty lines.
    """
    line = line.strip()
    if not line or not line.startswith("%"):
        return None

    # %output %<pane_id> <data>
    if line.startswith("%output "):
        parts = line.split(" ", 2)
        if len(parts) < 3:
            return None
        pane_id = parts[1]
        data = _unescape_output(parts[2])
        return OutputEvent(pane_id=pane_id, data=data)

    # %pane-mode-changed %<pane_id>
    if line.startswith("%pane-mode-changed "):
        parts = line.split(" ", 1)
        if len(parts) < 2:
            return None
        return ModeChangeEvent(pane_id=parts[1])

    # %layout-change <window_id> <layout>
    if line.startswith("%layout-change "):
        parts = line.split(" ", 2)
        if len(parts) < 3:
            return None
        return LayoutChangeEvent(window_id=parts[1], layout=parts[2])

    # %window-add / %window-close
    if line.startswith("%window-add ") or line.startswith("%window-close "):
        parts = line.split(" ", 1)
        action = "add" if "add" in parts[0] else "close"
        window_id = parts[1] if len(parts) > 1 else ""
        return WindowEvent(window_id=window_id, action=action)

    # %session-changed $<id> <name>
    if line.startswith("%session-changed "):
        parts = line.split(" ", 2)
        if len(parts) < 3:
            return None
        return SessionEvent(session_id=parts[1], name=parts[2])

    # %exit [reason]
    if line.startswith("%exit"):
        parts = line.split(" ", 1)
        reason = parts[1] if len(parts) > 1 else ""
        return ExitEvent(reason=reason)

    # %begin, %end, %error — internal markers, skip
    if line.startswith(("%begin", "%end", "%error")):
        return None

    return None


class ControlModeConnection:
    """A single control mode connection to a tmux session.

    Manages the subprocess lifecycle and provides an async event stream.
    Automatically reconnects on unexpected disconnects.
    """

    def __init__(self, session_name: str, config: ControlModeConfig) -> None:
        self._session_name = session_name
        self._config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._state = ConnectionState.DISCONNECTED
        self._stop_event = asyncio.Event()

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def session_name(self) -> str:
        return self._session_name

    async def start(self) -> None:
        """Spawn the control mode subprocess."""
        if self._proc is not None and self._proc.returncode is None:
            return  # Already running

        self._stop_event.clear()
        self._proc = await asyncio.create_subprocess_exec(
            "tmux",
            "-C",
            "attach-session",
            "-t",
            self._session_name,
            "-r",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._state = ConnectionState.CONNECTED
        log.info("Control mode connected to '%s' (pid=%s)", self._session_name, self._proc.pid)

    async def stop(self) -> None:
        """Stop the connection gracefully."""
        self._stop_event.set()
        if self._proc is None or self._proc.returncode is not None:
            self._state = ConnectionState.DISCONNECTED
            return

        try:
            self._proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        except ProcessLookupError:
            pass  # Already exited

        self._state = ConnectionState.DISCONNECTED
        log.info("Control mode disconnected from '%s'", self._session_name)

    async def _reconnect(self) -> bool:
        """Attempt reconnection with exponential backoff.

        Returns True if reconnected, False if max attempts exhausted.
        """
        self._state = ConnectionState.RECONNECTING
        for attempt in range(1, self._config.max_reconnect_attempts + 1):
            if self._stop_event.is_set():
                return False

            delay = self._config.reconnect_interval_seconds * (
                self._config.reconnect_backoff_factor ** (attempt - 1)
            )
            log.info(
                "Reconnecting to '%s' (attempt %d/%d, delay %.1fs)",
                self._session_name,
                attempt,
                self._config.max_reconnect_attempts,
                delay,
            )

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return False  # stop_event was set during sleep
            except TimeoutError:
                pass  # Sleep completed, try reconnecting

            try:
                await self.start()
                return True
            except Exception:
                log.warning(
                    "Reconnect attempt %d failed for '%s'",
                    attempt,
                    self._session_name,
                    exc_info=True,
                )

        self._state = ConnectionState.FAILED
        log.error(
            "Max reconnect attempts reached for '%s'",
            self._session_name,
        )
        return False

    async def stream(self) -> AsyncIterator[ControlModeEvent]:
        """Yield control mode events from the subprocess.

        Transparently reconnects on unexpected EOF. The generator ends
        when stop() is called or max reconnect attempts are exhausted.
        """
        while not self._stop_event.is_set():
            if self._proc is None or self._proc.stdout is None:
                break

            try:
                raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=1.0)
            except TimeoutError:
                continue  # Check stop_event and loop

            if not raw:
                # EOF — subprocess exited
                if self._stop_event.is_set():
                    break  # Intentional stop
                # Unexpected disconnect — reconnect
                if not await self._reconnect():
                    break
                continue

            line = raw.decode("utf-8", errors="replace")
            event = parse_control_mode_line(line)
            if event is not None:
                yield event

                if isinstance(event, ExitEvent):
                    if self._stop_event.is_set():
                        break
                    if not await self._reconnect():
                        break


class ControlModeManager:
    """Manages control mode connections across sessions.

    Connections are expensive subprocess handles — this manager ensures
    a single connection per session and provides lifecycle control.
    """

    def __init__(self) -> None:
        self._connections: dict[str, ControlModeConnection] = {}

    async def connect(self, session_name: str, config: ControlModeConfig) -> ControlModeConnection:
        """Get or create a connection for a session."""
        existing = self._connections.get(session_name)
        if existing is not None and existing.state in (
            ConnectionState.CONNECTED,
            ConnectionState.RECONNECTING,
        ):
            return existing

        conn = ControlModeConnection(session_name, config)
        await conn.start()
        self._connections[session_name] = conn
        return conn

    async def disconnect(self, session_name: str) -> None:
        """Disconnect a session's control mode connection."""
        conn = self._connections.pop(session_name, None)
        if conn is not None:
            await conn.stop()

    def get_connection(self, session_name: str) -> ControlModeConnection | None:
        """Get an existing connection (if any)."""
        return self._connections.get(session_name)

    async def close_all(self) -> None:
        """Disconnect all active connections."""
        for conn in list(self._connections.values()):
            await conn.stop()
        self._connections.clear()


# Module-level singleton
_manager = ControlModeManager()
