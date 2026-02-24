"""Socket.IO server for interactive terminal communication.

Provides bidirectional real-time terminal access via PTY-based
`tmux attach-session`. Each viewed session gets a PTY that produces
the exact same byte stream as a native terminal (Ghostty, iTerm).

Control mode infrastructure (StreamBroker, SSE) remains for session
monitoring and dashboard state events — only the interactive terminal
path uses PTY.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import socketio

from src.pty_manager import PtyManager
from src.tmux import capture_pane, list_panes, session_exists

log = logging.getLogger(__name__)

# Module-level PTY manager singleton
_pty_manager = PtyManager()

# --- Hardening constants ---
MAX_INPUT_BYTES = 4096
MIN_COLS, MAX_COLS = 10, 500
MIN_ROWS, MAX_ROWS = 2, 200
# Rate limiting: max input events per second per sid+session
INPUT_RATE_LIMIT = 100
INPUT_RATE_WINDOW = 1.0  # seconds


def get_pty_manager() -> PtyManager:
    """Get the shared PtyManager instance."""
    return _pty_manager


def create_sio(cors_origins: list[str]) -> socketio.AsyncServer:
    """Create and configure the Socket.IO async server."""
    sio = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins=cors_origins,
    )
    sio.register_namespace(TerminalNamespace("/terminal"))
    return sio


class TerminalNamespace(socketio.AsyncNamespace):
    """Socket.IO namespace for interactive terminal sessions.

    Uses PTY-based `tmux attach-session` for full terminal fidelity:
    correct ANSI rendering, input echo, and proper resize propagation.

    Events:
        connect     — authenticate via API key in auth dict
        join        — attach to a session via PTY
        leave       — detach from a session
        input       — write keyboard input to PTY
        resize      — resize PTY (SIGWINCH to tmux attach)
        disconnect  — clean up all sessions
    """

    def __init__(self, namespace: str) -> None:
        super().__init__(namespace)
        # sid -> {session_name: forwarding_task}
        self._subscriptions: dict[str, dict[str, asyncio.Task]] = {}
        # Rate limiting: (sid, session) -> list of timestamps
        self._input_timestamps: dict[tuple[str, str], list[float]] = {}

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool:
        """Validate API key on connection.

        Auth dict should contain {"api_key": "..."}.
        Dev mode (BACKBONE_API_KEY not set) allows all connections.
        """
        api_key = os.environ.get("BACKBONE_API_KEY", "")
        if not api_key:
            return True  # Dev mode — unrestricted

        if not auth or auth.get("api_key") != api_key:
            log.warning("Socket.IO connection rejected — invalid auth (sid=%s)", sid)
            return False

        return True

    async def on_join(self, sid: str, data: dict) -> None:
        """Join a terminal session via PTY.

        data: {"session": "session-name"}

        Creates or reuses a PTY running `tmux attach-session`,
        subscribes this client, sends initial snapshot with pane
        dimensions, and starts output forwarding.
        """
        session_name = data.get("session", "")
        if not session_name:
            await self.emit("error", {"message": "Missing session name"}, to=sid)
            return

        if not await session_exists(session_name):
            await self.emit("error", {"message": f"Session '{session_name}' not found"}, to=sid)
            return

        # Get pane dimensions for initial PTY size
        panes = await list_panes(session_name)
        active_pane = next((p for p in panes if p["pane_active"]), None)
        cols = int(active_pane["pane_width"]) if active_pane else 80
        rows = int(active_pane["pane_height"]) if active_pane else 24

        # Get or create PTY session
        mgr = get_pty_manager()
        pty_session = mgr.get_or_create(session_name, cols, rows)
        queue = pty_session.subscribe(sid)

        # Enter Socket.IO room
        self.enter_room(sid, f"session:{session_name}")

        # Send initial snapshot with pane dimensions
        snapshot = await capture_pane(session_name, lines=200)
        await self.emit(
            "snapshot",
            {"session": session_name, "data": snapshot, "cols": cols, "rows": rows},
            to=sid,
        )

        # Start forwarding PTY output to this client
        task = asyncio.create_task(
            self._forward_pty_output(sid, session_name, queue),
            name=f"sio-pty-{sid}-{session_name}",
        )

        # Track subscription
        if sid not in self._subscriptions:
            self._subscriptions[sid] = {}
        self._subscriptions[sid][session_name] = task

        log.info("sid=%s joined session '%s' via PTY", sid, session_name)

    async def on_leave(self, sid: str, data: dict) -> None:
        """Leave a terminal session.

        data: {"session": "session-name"}
        """
        session_name = data.get("session", "")
        await self._cleanup_session(sid, session_name)

    async def on_input(self, sid: str, data: dict) -> None:
        """Write keyboard input to PTY.

        data: {"session": "session-name", "data": "input-bytes"}

        Writes directly to the PTY master fd — instant, with echo
        handled by the PTY line discipline.
        """
        session_name = data.get("session", "")
        input_data = data.get("data", "")

        if not session_name or not input_data:
            return

        # Gap 1: Input size validation
        if len(input_data) > MAX_INPUT_BYTES:
            log.warning(
                "Input too large from sid=%s (%d bytes, max %d)",
                sid, len(input_data), MAX_INPUT_BYTES,
            )
            input_data = input_data[:MAX_INPUT_BYTES]

        # Verify sid is subscribed to this session
        sid_subs = self._subscriptions.get(sid, {})
        if session_name not in sid_subs:
            await self.emit(
                "error", {"message": f"Not joined to session '{session_name}'"}, to=sid
            )
            return

        # Gap 4: Rate limiting
        if self._is_rate_limited(sid, session_name):
            return

        mgr = get_pty_manager()
        pty_session = mgr.get(session_name)
        if pty_session:
            pty_session.write(input_data)

    async def on_resize(self, sid: str, data: dict) -> None:
        """Resize PTY to match client terminal dimensions.

        data: {"session": "session-name", "cols": 140, "rows": 35}

        Uses TIOCSWINSZ ioctl which sends SIGWINCH to the tmux attach
        process. Only affects this client's view, not other tmux clients.
        """
        session_name = data.get("session", "")
        cols = data.get("cols")
        rows = data.get("rows")

        if not session_name or not cols or not rows:
            return

        # Verify sid is subscribed to this session
        sid_subs = self._subscriptions.get(sid, {})
        if session_name not in sid_subs:
            await self.emit(
                "error", {"message": f"Not joined to session '{session_name}'"}, to=sid
            )
            return

        # Gap 3: Resize bounds validation
        try:
            clamped_cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
            clamped_rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))
        except (ValueError, TypeError):
            return

        mgr = get_pty_manager()
        pty_session = mgr.get(session_name)
        if pty_session:
            pty_session.resize(clamped_cols, clamped_rows)

    async def on_disconnect(self, sid: str) -> None:
        """Clean up all subscriptions for a disconnecting client."""
        sessions = list(self._subscriptions.get(sid, {}).keys())
        for session_name in sessions:
            await self._cleanup_session(sid, session_name)
        self._subscriptions.pop(sid, None)
        log.info("sid=%s disconnected, cleaned up %d sessions", sid, len(sessions))

    async def _cleanup_session(self, sid: str, session_name: str) -> None:
        """Unsubscribe from PTY and clean up if last subscriber."""
        sid_subs = self._subscriptions.get(sid, {})
        task = sid_subs.pop(session_name, None)
        if task is None:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mgr = get_pty_manager()
        pty_session = mgr.get(session_name)
        if pty_session:
            pty_session.unsubscribe(sid)
            if pty_session.subscriber_count == 0:
                mgr.remove(session_name)

        self.leave_room(sid, f"session:{session_name}")
        self._input_timestamps.pop((sid, session_name), None)
        log.info("sid=%s left session '%s'", sid, session_name)

    def _is_rate_limited(self, sid: str, session_name: str) -> bool:
        """Check if input from this sid+session exceeds the rate limit."""
        key = (sid, session_name)
        now = time.monotonic()
        timestamps = self._input_timestamps.get(key, [])

        # Prune timestamps outside the window
        cutoff = now - INPUT_RATE_WINDOW
        timestamps = [t for t in timestamps if t > cutoff]
        timestamps.append(now)
        self._input_timestamps[key] = timestamps

        if len(timestamps) > INPUT_RATE_LIMIT:
            log.warning(
                "Rate limit exceeded for sid=%s session='%s' (%d/%d per %.0fs)",
                sid, session_name, len(timestamps),
                INPUT_RATE_LIMIT, INPUT_RATE_WINDOW,
            )
            return True
        return False

    async def _forward_pty_output(
        self,
        sid: str,
        session_name: str,
        queue: asyncio.Queue[str | None],
    ) -> None:
        """Forward PTY output to Socket.IO client.

        Reads raw terminal bytes from the PTY subscriber queue and
        emits them as terminal_output events. Handles backpressure
        by catching emit failures for slow clients.
        """
        try:
            while True:
                data = await queue.get()
                if data is None:
                    break
                try:
                    await self.emit(
                        "terminal_output",
                        {"session": session_name, "data": data},
                        to=sid,
                    )
                except Exception:
                    log.warning(
                        "Output emit failed for sid=%s session='%s' "
                        "(client may be slow)",
                        sid, session_name,
                    )
        except asyncio.CancelledError:
            return
