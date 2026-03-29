"""Socket.IO server for interactive terminal communication.

Provides bidirectional real-time terminal access via PTY-based
`tmux attach-session`. Each WebSocket connection gets its own PTY
(1:1 model), delegating multiplexing to tmux natively.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time

import socketio

from agent_backbone.api.session_updates import SESSIONS_NAMESPACE
from agent_backbone.services.terminal import (
    PtyManager,
    list_panes,
    resize_window,
    session_exists,
    set_window_size_mode,
)

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
# Output coalescing window (milliseconds)
COALESCE_MS = 3


def get_pty_manager() -> PtyManager:
    """Get the shared PtyManager instance."""
    return _pty_manager


def create_sio(cors_origins: list[str]) -> socketio.AsyncServer:
    """Create and configure the Socket.IO async server."""
    sio = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins=cors_origins,
    )
    sio.register_namespace(SessionsNamespace(SESSIONS_NAMESPACE))
    sio.register_namespace(TerminalNamespace("/terminal"))
    return sio


async def _restore_dynamic_window_size(session_name: str) -> None:
    """Return tmux size selection to active-client control.

    Browser-driven `resize-window` calls are useful for immediate reflow, but tmux
    leaves the target window in `window-size manual`. That pins the session even
    after the browser collapses or detaches. Resetting to `latest` matches native
    terminal behavior: the most recent non-ignored client becomes the size authority.
    """
    if not await set_window_size_mode(session_name, "latest"):
        log.error("Failed to restore tmux window-size latest for '%s'", session_name)


def _socket_auth_valid(auth: dict | None = None) -> bool:
    """Validate Socket.IO auth against the configured API key."""
    api_key = os.environ.get("BACKBONE_API_KEY", "")
    if not api_key:
        return True
    raw = auth.get("api_key") if isinstance(auth, dict) else None
    token = raw if isinstance(raw, str) else ""
    return hmac.compare_digest(token, api_key)


class SessionsNamespace(socketio.AsyncNamespace):
    """Socket.IO namespace for enriched session state subscriptions.

    Supports room-based filtering per SOCKET_IO_SUBSCRIPTIONS protocol:
    - agent:{session} rooms for per-agent updates
    - run:{runId} rooms for run events
    - all-agents room for full broadcast (default)
    """

    def __init__(self, namespace: str | None = None) -> None:
        super().__init__(namespace)
        # Track whether a client has explicitly subscribed (vs default all-agents).
        self._explicit_subscribers: set[str] = set()

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool:
        """Validate API key and join default room (SUB-6, SUB-12)."""
        if not _socket_auth_valid(auth):
            log.warning("Socket.IO sessions connection rejected — invalid auth (sid=%s)", sid)
            return False
        # SUB-6: Default to all-agents for backwards compatibility
        self.enter_room(sid, "all-agents")
        return True

    async def on_subscribe(self, sid: str, data: dict) -> None:
        """Handle subscribe event — join rooms (SUB-2, SUB-3, SUB-7)."""
        if not isinstance(data, dict):
            return

        for agent in data.get("agents", []):
            if isinstance(agent, str) and agent:
                self.enter_room(sid, f"agent:{agent}")

        for run_id in data.get("runs", []):
            if isinstance(run_id, str) and run_id:
                self.enter_room(sid, f"run:{run_id}")

        if data.get("all_agents"):
            self.enter_room(sid, "all-agents")
        elif data.get("agents") or data.get("runs"):
            # SUB-7: Remove default all-agents if client subscribes
            # to specific agents/runs without requesting all_agents
            if sid not in self._explicit_subscribers:
                self.leave_room(sid, "all-agents")

        self._explicit_subscribers.add(sid)

        # SUB-5: Acknowledge with current room list
        rooms = self.rooms(sid) or []
        filtered = [r for r in rooms if r != sid]
        await self.emit("subscribed", {"rooms": filtered}, to=sid)

    async def on_unsubscribe(self, sid: str, data: dict) -> None:
        """Handle unsubscribe event — leave rooms (SUB-4)."""
        if not isinstance(data, dict):
            return

        for agent in data.get("agents", []):
            if isinstance(agent, str) and agent:
                self.leave_room(sid, f"agent:{agent}")

        for run_id in data.get("runs", []):
            if isinstance(run_id, str) and run_id:
                self.leave_room(sid, f"run:{run_id}")

        if data.get("all_agents"):
            self.leave_room(sid, "all-agents")

        # SUB-5: Acknowledge with current room list
        rooms = self.rooms(sid) or []
        filtered = [r for r in rooms if r != sid]
        await self.emit("subscribed", {"rooms": filtered}, to=sid)

    async def on_disconnect(self, sid: str) -> None:
        """Clean up tracking on disconnect (SUB-13)."""
        self._explicit_subscribers.discard(sid)


class TerminalNamespace(socketio.AsyncNamespace):
    """Socket.IO namespace for interactive terminal sessions.

    Uses PTY-based `tmux attach-session` for full terminal fidelity:
    correct ANSI rendering, input echo, and proper resize propagation.

    1:1 model: each WebSocket connection gets its own PTY process.

    Events:
        connect      — authenticate via API key in auth dict
        join         — attach to a session via PTY
        leave        — detach from a session
        input        — write keyboard input to PTY
        resize       — resize PTY (SIGWINCH to tmux attach)
        release_dims — release browser tmux size control (dashboard collapse)
        disconnect   — clean up all sessions
    """

    def __init__(self, namespace: str) -> None:
        super().__init__(namespace)
        # sid -> {session_name: forwarding_task or None when collapsed/detached}
        self._subscriptions: dict[str, dict[str, asyncio.Task | None]] = {}
        # Rate limiting: (sid, session) -> list of timestamps
        self._input_timestamps: dict[tuple[str, str], list[float]] = {}
        # Attached browser PTY clients: session_name -> set of sids
        self._active_sessions: dict[str, set[str]] = {}
        # Read-only subscribers: sid -> set of session_names
        self._readonly: dict[str, set[str]] = {}

    async def _attach_subscription_client(
        self,
        sid: str,
        session_name: str,
        cols: int,
        rows: int,
    ) -> None:
        """Create a PTY client and forwarding task for an existing subscription."""
        mgr = get_pty_manager()
        pty_session = await mgr.create(sid, session_name, cols, rows)

        def _on_drop(sn: str, _sid: str = sid) -> None:
            self._on_data_dropped(_sid, sn)

        pty_session.on_data_dropped = _on_drop

        task = asyncio.create_task(
            self._forward_pty_output(sid, session_name, pty_session.output_queue),
            name=f"sio-pty-{sid}-{session_name}",
        )
        self._subscriptions.setdefault(sid, {})[session_name] = task
        self._active_sessions.setdefault(session_name, set()).add(sid)

    async def _detach_subscription_client(self, sid: str, session_name: str) -> None:
        """Detach the browser PTY client but keep the logical subscription."""
        sid_subs = self._subscriptions.get(sid, {})
        if session_name not in sid_subs:
            return

        task = sid_subs.get(session_name)
        if isinstance(task, asyncio.Task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.warning("PTY forwarding task failed during detach: %s", e)
        elif task is not None:
            cancel = getattr(task, "cancel", None)
            if callable(cancel):
                cancel()

        mgr = get_pty_manager()
        await mgr.remove(sid, session_name)
        sid_subs[session_name] = None

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool:
        """Validate API key on connection.

        Auth dict should contain {"api_key": "..."}.
        Dev mode (BACKBONE_API_KEY not set) allows all connections.
        """
        if not _socket_auth_valid(auth):
            log.warning("Socket.IO connection rejected — invalid auth (sid=%s)", sid)
            return False

        return True

    async def on_join(self, sid: str, data: dict) -> None:
        """Join a terminal session via PTY.

        data: {"session": "session-name", "cols": 160, "rows": 35, "readonly": false}

        Creates a dedicated PTY running `tmux attach-session` for this
        client and starts output forwarding. When a fresh tmux attach
        connects, tmux natively redraws the full screen.

        When the client provides cols/rows, those are used for PTY
        sizing (clamped to MIN/MAX bounds). Otherwise falls back to
        the tmux pane's native dimensions.

        If readonly=true, the client receives output but cannot send input.
        """
        session_name = data.get("session", "")
        if not session_name:
            await self.emit("error", {"message": "Missing session name"}, to=sid)
            return

        if not await session_exists(session_name):
            await self.emit("error", {"message": f"Session '{session_name}' not found"}, to=sid)
            return

        # Fix double-join: clean up existing subscription before creating a new one
        if session_name in self._subscriptions.get(sid, {}):
            await self._cleanup_session(sid, session_name)

        readonly = bool(data.get("readonly", False))

        # Determine PTY dimensions: prefer client-provided, fall back to tmux pane
        client_cols = data.get("cols")
        client_rows = data.get("rows")

        if client_cols is not None and client_rows is not None:
            try:
                cols = max(MIN_COLS, min(MAX_COLS, int(client_cols)))
                rows = max(MIN_ROWS, min(MAX_ROWS, int(client_rows)))
            except (ValueError, TypeError):
                cols, rows = 80, 24
        else:
            panes = await list_panes(session_name)
            active_pane = next((p for p in panes if p["pane_active"]), None)
            cols = int(active_pane["pane_width"]) if active_pane else 80
            rows = int(active_pane["pane_height"]) if active_pane else 24

        try:
            # Enter Socket.IO room
            await self.enter_room(sid, f"session:{session_name}")
            await self._attach_subscription_client(sid, session_name, cols, rows)
            if readonly:
                self._readonly.setdefault(sid, set()).add(session_name)
        except Exception:
            # Client disconnected or error before tracking — clean up orphan
            await get_pty_manager().remove(sid, session_name)
            raise

        log.info(
            "sid=%s joined session '%s' via PTY%s",
            sid,
            session_name,
            " (readonly)" if readonly else "",
        )

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
        handled by the PTY line discipline. Rejects input from
        read-only subscribers.
        """
        session_name = data.get("session", "")
        input_data = data.get("data", "")

        if not session_name or not input_data:
            return

        # Gap 1: Input size validation
        if len(input_data) > MAX_INPUT_BYTES:
            log.warning(
                "Input too large from sid=%s (%d bytes, max %d)",
                sid,
                len(input_data),
                MAX_INPUT_BYTES,
            )
            input_data = input_data[:MAX_INPUT_BYTES]

        # Verify sid is subscribed to this session
        sid_subs = self._subscriptions.get(sid, {})
        if session_name not in sid_subs:
            await self.emit("error", {"message": f"Not joined to session '{session_name}'"}, to=sid)
            return

        # Reject input from read-only subscribers
        if session_name in self._readonly.get(sid, set()):
            await self.emit(
                "error",
                {"message": "Read-only session — input not allowed"},
                to=sid,
            )
            return

        # Gap 4: Rate limiting
        if self._is_rate_limited(sid, session_name):
            return

        mgr = get_pty_manager()
        pty_session = mgr.get(sid, session_name)
        if pty_session:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, pty_session.write, input_data)

    async def on_resize(self, sid: str, data: dict) -> None:
        """Resize PTY to match client terminal dimensions.

        data: {"session": "session-name", "cols": 140, "rows": 35}

        Uses TIOCSWINSZ ioctl which sends SIGWINCH to the tmux attach
        process. In 1:1 model, directly resizes this client's PTY.

        When resuming from a released state (after release_dims), re-captures
        current tmux dimensions as the new original before applying the resize.
        """
        session_name = data.get("session", "")
        cols = data.get("cols")
        rows = data.get("rows")

        if not session_name or not cols or not rows:
            return

        # Verify sid is subscribed to this session
        sid_subs = self._subscriptions.get(sid, {})
        if session_name not in sid_subs:
            await self.emit("error", {"message": f"Not joined to session '{session_name}'"}, to=sid)
            return

        # Gap 3: Resize bounds validation
        try:
            clamped_cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
            clamped_rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))
        except (ValueError, TypeError):
            return

        mgr = get_pty_manager()
        pty_session = mgr.get(sid, session_name)
        if pty_session is None:
            await self._attach_subscription_client(sid, session_name, clamped_cols, clamped_rows)
            log.info(
                "Reattached browser PTY for '%s' after collapse (%dx%d)",
                session_name,
                clamped_cols,
                clamped_rows,
            )
            return

        pty_session.resize(clamped_cols, clamped_rows)
        if not await resize_window(session_name, clamped_cols, clamped_rows):
            log.error(
                "resize_window failed on resize for '%s' (%dx%d)",
                session_name,
                clamped_cols,
                clamped_rows,
            )
        await _restore_dynamic_window_size(session_name)

    async def on_release_dims(self, sid: str, data: dict) -> None:
        """Release browser size control without disconnecting.

        data: {"session": "session-name"}

        Called when the dashboard collapses/hides a terminal. Fully detaches
        the browser PTY client so native terminals immediately regain tmux
        size authority. The socket stays alive and the logical subscription
        remains, so a subsequent resize event will create a fresh PTY client.
        """
        session_name = data.get("session", "")
        if not session_name:
            return

        # Verify sid is subscribed to this session
        if session_name not in self._subscriptions.get(sid, {}):
            await self.emit(
                "error",
                {"message": f"Not joined to session '{session_name}'"},
                to=sid,
            )
            return

        await self._detach_subscription_client(sid, session_name)
        active_sids = self._active_sessions.get(session_name, set())
        active_sids.discard(sid)
        if not active_sids:
            self._active_sessions.pop(session_name, None)

        log.info(
            "Detached browser PTY for '%s' on collapse",
            session_name,
        )

    async def on_disconnect(self, sid: str) -> None:
        """Clean up all subscriptions for a disconnecting client."""
        sessions = list(self._subscriptions.get(sid, {}).keys())
        for session_name in sessions:
            await self._cleanup_session(sid, session_name)
        self._subscriptions.pop(sid, None)
        log.info("sid=%s disconnected, cleaned up %d sessions", sid, len(sessions))

    async def _cleanup_session(self, sid: str, session_name: str) -> None:
        """Remove this client's PTY/subscription state entirely."""
        sid_subs = self._subscriptions.get(sid, {})
        if session_name not in sid_subs:
            return

        await self._detach_subscription_client(sid, session_name)
        sid_subs.pop(session_name, None)
        if not sid_subs:
            self._subscriptions.pop(sid, None)

        # Remove from active sessions tracking
        active_sids = self._active_sessions.get(session_name, set())
        active_sids.discard(sid)

        # Clean up read-only tracking
        readonly_sessions = self._readonly.get(sid, set())
        readonly_sessions.discard(session_name)
        if not readonly_sessions:
            self._readonly.pop(sid, None)

        if not active_sids:
            self._active_sessions.pop(session_name, None)

        await self.leave_room(sid, f"session:{session_name}")
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
                sid,
                session_name,
                len(timestamps),
                INPUT_RATE_LIMIT,
                INPUT_RATE_WINDOW,
            )
            return True
        return False

    async def on_pause(self, sid: str, data: dict) -> None:
        """Client signals its write buffer is full — pause output.

        data: {"session": "session-name"}

        Pauses this client's PTY read loop, applying natural backpressure
        via the kernel buffer.
        """
        session_name = data.get("session", "")
        if not session_name:
            return
        mgr = get_pty_manager()
        pty_session = mgr.get(sid, session_name)
        if pty_session:
            pty_session.pause()

    async def on_resume(self, sid: str, data: dict) -> None:
        """Client signals its write buffer has drained — resume output.

        data: {"session": "session-name"}
        """
        session_name = data.get("session", "")
        if not session_name:
            return
        mgr = get_pty_manager()
        pty_session = mgr.get(sid, session_name)
        if pty_session:
            pty_session.resume()

    async def on_focus(self, sid: str, data: dict) -> None:
        """Forward browser tab focus/blur to the PTY as DECSET 1004 sequences.

        data: {"session": "session-name", "focused": true/false}

        Programs that enable focus reporting (DECSET 1004) will receive
        these sequences and can react to focus changes.
        """
        session_name = data.get("session", "")
        focused = data.get("focused", True)
        if not session_name:
            return

        # Verify sid is subscribed
        if session_name not in self._subscriptions.get(sid, {}):
            return

        # Reject from read-only
        if session_name in self._readonly.get(sid, set()):
            return

        mgr = get_pty_manager()
        pty_session = mgr.get(sid, session_name)
        if pty_session:
            # DECSET 1004 focus event sequences
            pty_session.write("\x1b[I" if focused else "\x1b[O")

    def _on_data_dropped(self, sid: str, session_name: str) -> None:
        """Callback from PtySession when queue overflow drops data.

        Schedules an async emit of a data_dropped event to the affected client.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(
                    self.emit(
                        "data_dropped",
                        {"session": session_name},
                        to=sid,
                    ),
                    name=f"data-dropped-{sid}-{session_name}",
                )
        except RuntimeError:
            pass

    async def _forward_pty_output(
        self,
        sid: str,
        session_name: str,
        queue: asyncio.Queue[str | None],
    ) -> None:
        """Forward PTY output to Socket.IO client with output coalescing.

        Buffers output for COALESCE_MS milliseconds and sends as a single
        concatenated chunk. This reduces Socket.IO event count for rapid
        small outputs (keystroke echoes, character-at-a-time output).

        Emits a session_ended event when the PTY process dies (None sentinel).
        """
        try:
            while True:
                data = await queue.get()
                if data is None:
                    try:
                        await self.emit(
                            "session_ended",
                            {"session": session_name, "reason": "process_exited"},
                            to=sid,
                        )
                    except Exception:
                        log.debug("session_ended emit failed for sid=%s (client gone)", sid)
                    break

                buffer = [data]

                # Drain any immediately available items
                while not queue.empty():
                    more = queue.get_nowait()
                    if more is None:
                        # Flush buffer then send session_ended
                        if buffer:
                            try:
                                await self.emit(
                                    "terminal_output",
                                    {"session": session_name, "data": "".join(buffer)},
                                    to=sid,
                                )
                            except Exception:
                                log.debug("terminal_output flush failed for sid=%s", sid)
                        try:
                            await self.emit(
                                "session_ended",
                                {"session": session_name, "reason": "process_exited"},
                                to=sid,
                            )
                        except Exception:
                            log.debug("session_ended emit failed for sid=%s (client gone)", sid)
                        return
                    buffer.append(more)

                # Coalesce: wait briefly for more data
                try:
                    async with asyncio.timeout(COALESCE_MS / 1000):
                        more = await queue.get()
                        if more is None:
                            if buffer:
                                try:
                                    await self.emit(
                                        "terminal_output",
                                        {"session": session_name, "data": "".join(buffer)},
                                        to=sid,
                                    )
                                except Exception:
                                    log.debug("terminal_output flush failed for sid=%s", sid)
                            try:
                                await self.emit(
                                    "session_ended",
                                    {"session": session_name, "reason": "process_exited"},
                                    to=sid,
                                )
                            except Exception:
                                log.debug("session_ended emit failed for sid=%s (client gone)", sid)
                            return
                        buffer.append(more)
                except TimeoutError:
                    pass

                # Emit coalesced chunk
                try:
                    await self.emit(
                        "terminal_output",
                        {"session": session_name, "data": "".join(buffer)},
                        to=sid,
                    )
                except Exception:
                    log.warning(
                        "Output emit failed for sid=%s session='%s' (client may be slow)",
                        sid,
                        session_name,
                    )
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.exception("Unhandled exception in _forward_pty_output for sid=%s: %s", sid, e)
