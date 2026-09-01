"""Socket.IO server — live state feed and read-only terminal streaming.

Namespaces:

* ``/sessions`` — receives ``sessions:update`` snapshots whenever agent state
  changes (emitted by the API and the monitor job).
* ``/terminal`` — read-only stream of a registered agent's terminal. Each subscriber
  gets its own PTY running ``tmux attach-session`` so rendering is faithful,
  but input is never forwarded: the backbone streams terminals, it does not
  let remote clients type into them.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import socketio

from agent_backbone.api.auth import api_key_valid
from agent_backbone.api.session_updates import SESSIONS_NAMESPACE
from agent_backbone.services.terminal import (
    PtyManager,
    active_pane_size,
    resize_window,
    session_exists,
    set_window_size_mode,
)

log = logging.getLogger(__name__)

_pty_manager: PtyManager | None = None

MIN_COLS, MAX_COLS = 10, 500
MIN_ROWS, MAX_ROWS = 2, 200
COALESCE_MS = 3


def configure_pty_manager(data_dir: Path) -> None:
    """Create the shared PTY manager, tracking attach processes under ``data_dir``."""
    global _pty_manager
    _pty_manager = PtyManager(data_dir / "pty-pids.txt")


def get_pty_manager() -> PtyManager:
    """The shared PTY manager (created on first use when not configured)."""
    global _pty_manager
    if _pty_manager is None:
        _pty_manager = PtyManager()
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
    """Return tmux size selection to active-client control after a browser resize."""
    if not await set_window_size_mode(session_name, "latest"):
        log.error("Failed to restore tmux window-size latest for '%s'", session_name)


class _AuthenticatedNamespace(socketio.AsyncNamespace):
    """Namespace base that validates ``auth["api_key"]`` against the app config."""

    def _config(self):
        app = getattr(self.server, "fastapi_app", None)
        return getattr(getattr(app, "state", None), "config", None)

    def _auth_valid(self, auth: dict | None) -> bool:
        config = self._config()
        if config is None:
            return False
        if not config.api_key:
            return config.security.allow_unauthenticated
        raw = auth.get("api_key") if isinstance(auth, dict) else None
        return api_key_valid(raw if isinstance(raw, str) else None, config.api_key)

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool:
        if not self._auth_valid(auth):
            log.warning(
                "Socket.IO %s connection rejected — invalid auth (sid=%s)", self.namespace, sid
            )
            return False
        return True


class SessionsNamespace(_AuthenticatedNamespace):
    """Socket.IO namespace for enriched session state subscriptions.

    Updates are change-driven; a full snapshot is sent to each client on
    connect so subscribers never start blind.
    """

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool:
        if not await super().on_connect(sid, environ, auth):
            return False
        from agent_backbone.api.session_updates import (
            SESSIONS_UPDATE_EVENT,
            build_session_snapshot,
            get_cached_session_snapshot,
        )

        app = getattr(self.server, "fastapi_app", None)
        state = getattr(app, "state", None)
        config = getattr(state, "config", None)
        state_svc = getattr(state, "state_service", None)
        tmux_svc = getattr(state, "tmux_service", None)
        if config is None or state_svc is None or tmux_svc is None:
            return True
        try:
            snapshot = await get_cached_session_snapshot(
                lambda: build_session_snapshot(config, state_svc, tmux_svc)
            )
            payload = [agent.model_dump(mode="json") for agent in snapshot]
            await self.emit(SESSIONS_UPDATE_EVENT, payload, to=sid)
        except Exception:
            log.exception("Could not send the initial /sessions snapshot (non-fatal)")
        return True


class TerminalNamespace(_AuthenticatedNamespace):
    """Read-only terminal streaming.

    Events:
        connect      — authenticate via api_key in auth dict
        join         — attach to a session via PTY {"session", "cols", "rows"}
        leave        — detach from a session
        resize       — resize PTY (SIGWINCH to tmux attach)
        release_dims — release browser tmux size control (dashboard collapse)
        pause/resume — client-side backpressure
        disconnect   — clean up all sessions
    """

    def __init__(self, namespace: str) -> None:
        super().__init__(namespace)
        # sid -> {session_name: forwarding_task or None when collapsed/detached}
        self._subscriptions: dict[str, dict[str, asyncio.Task | None]] = {}
        self._active_sessions: dict[str, set[str]] = {}
        self._background: set[asyncio.Task] = set()

    async def _attach_subscription_client(
        self, sid: str, session_name: str, cols: int, rows: int
    ) -> None:
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

        await get_pty_manager().remove(sid, session_name)
        sid_subs[session_name] = None

    async def on_join(self, sid: str, data: dict) -> None:
        session_name = data.get("session", "")
        if not session_name:
            await self.emit("error", {"message": "Missing session name"}, to=sid)
            return

        # Only registered agents are streamed — never an arbitrary tmux
        # session of the same user (same rule as GET /sessions/{name}/terminal).
        config = self._config()
        if config is None or config.agents.get(session_name) is None:
            await self.emit(
                "error", {"message": f"'{session_name}' is not a registered agent"}, to=sid
            )
            return
        if not await session_exists(session_name):
            await self.emit("error", {"message": f"Session '{session_name}' not found"}, to=sid)
            return

        if session_name in self._subscriptions.get(sid, {}):
            await self._cleanup_session(sid, session_name)

        client_cols = data.get("cols")
        client_rows = data.get("rows")
        if client_cols is not None and client_rows is not None:
            try:
                cols = max(MIN_COLS, min(MAX_COLS, int(client_cols)))
                rows = max(MIN_ROWS, min(MAX_ROWS, int(client_rows)))
            except (ValueError, TypeError):
                cols, rows = 80, 24
        else:
            cols, rows = await active_pane_size(session_name) or (80, 24)

        try:
            await self.enter_room(sid, f"session:{session_name}")
            await self._attach_subscription_client(sid, session_name, cols, rows)
        except Exception:
            await get_pty_manager().remove(sid, session_name)
            raise

        log.info("sid=%s joined session '%s' (read-only stream)", sid, session_name)

    async def on_leave(self, sid: str, data: dict) -> None:
        await self._cleanup_session(sid, data.get("session", ""))

    async def on_resize(self, sid: str, data: dict) -> None:
        session_name = data.get("session", "")
        cols = data.get("cols")
        rows = data.get("rows")
        if not session_name or not cols or not rows:
            return

        if session_name not in self._subscriptions.get(sid, {}):
            await self.emit("error", {"message": f"Not joined to session '{session_name}'"}, to=sid)
            return

        try:
            clamped_cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
            clamped_rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))
        except (ValueError, TypeError):
            return

        pty_session = get_pty_manager().get(sid, session_name)
        if pty_session is None:
            await self._attach_subscription_client(sid, session_name, clamped_cols, clamped_rows)
            return

        pty_session.resize(clamped_cols, clamped_rows)
        if not await resize_window(session_name, clamped_cols, clamped_rows):
            log.error("resize_window failed for '%s'", session_name)
        await _restore_dynamic_window_size(session_name)

    async def on_release_dims(self, sid: str, data: dict) -> None:
        session_name = data.get("session", "")
        if not session_name or session_name not in self._subscriptions.get(sid, {}):
            return
        await self._detach_subscription_client(sid, session_name)
        active_sids = self._active_sessions.get(session_name, set())
        active_sids.discard(sid)
        if not active_sids:
            self._active_sessions.pop(session_name, None)

    async def on_pause(self, sid: str, data: dict) -> None:
        pty_session = get_pty_manager().get(sid, data.get("session", ""))
        if pty_session:
            pty_session.pause()

    async def on_resume(self, sid: str, data: dict) -> None:
        pty_session = get_pty_manager().get(sid, data.get("session", ""))
        if pty_session:
            pty_session.resume()

    async def on_disconnect(self, sid: str) -> None:
        sessions = list(self._subscriptions.get(sid, {}).keys())
        for session_name in sessions:
            await self._cleanup_session(sid, session_name)
        self._subscriptions.pop(sid, None)
        log.info("sid=%s disconnected, cleaned up %d sessions", sid, len(sessions))

    async def _cleanup_session(self, sid: str, session_name: str) -> None:
        sid_subs = self._subscriptions.get(sid, {})
        if session_name not in sid_subs:
            return

        await self._detach_subscription_client(sid, session_name)
        sid_subs.pop(session_name, None)
        if not sid_subs:
            self._subscriptions.pop(sid, None)

        active_sids = self._active_sessions.get(session_name, set())
        active_sids.discard(sid)
        if not active_sids:
            self._active_sessions.pop(session_name, None)

        await self.leave_room(sid, f"session:{session_name}")

    def _on_data_dropped(self, sid: str, session_name: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self.emit("data_dropped", {"session": session_name}, to=sid),
            name=f"data-dropped-{sid}-{session_name}",
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _emit_output(self, sid: str, session_name: str, buffer: list[str]) -> None:
        if not buffer:
            return
        try:
            await self.emit(
                "terminal_output", {"session": session_name, "data": "".join(buffer)}, to=sid
            )
        except Exception:
            log.debug("terminal_output emit failed for sid=%s (client gone?)", sid)

    async def _emit_ended(self, sid: str, session_name: str) -> None:
        try:
            await self.emit(
                "session_ended", {"session": session_name, "reason": "process_exited"}, to=sid
            )
        except Exception:
            log.debug("session_ended emit failed for sid=%s (client gone)", sid)

    async def _forward_pty_output(
        self,
        sid: str,
        session_name: str,
        queue: asyncio.Queue[str | None],
    ) -> None:
        """Forward PTY output to the client, coalescing rapid small chunks."""
        try:
            while True:
                data = await queue.get()
                if data is None:
                    await self._emit_ended(sid, session_name)
                    return

                buffer = [data]
                while not queue.empty():
                    more = queue.get_nowait()
                    if more is None:
                        await self._emit_output(sid, session_name, buffer)
                        await self._emit_ended(sid, session_name)
                        return
                    buffer.append(more)

                try:
                    async with asyncio.timeout(COALESCE_MS / 1000):
                        more = await queue.get()
                        if more is None:
                            await self._emit_output(sid, session_name, buffer)
                            await self._emit_ended(sid, session_name)
                            return
                        buffer.append(more)
                except TimeoutError:
                    pass

                await self._emit_output(sid, session_name, buffer)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.exception("Unhandled exception in _forward_pty_output for sid=%s: %s", sid, e)
