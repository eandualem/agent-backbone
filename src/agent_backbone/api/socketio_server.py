"""Socket.IO server — live state feed and read-only terminal streaming.

Namespaces:

* ``/sessions`` — receives ``sessions:update`` snapshots whenever agent state
  changes (emitted by the API and the monitor job), and ``inbox:pending``
  hints when an agent's inbox has more to read.
* ``/terminal`` — read-only stream of a registered agent's terminal. Each subscriber
  gets its own PTY running ``tmux attach-session`` so rendering is faithful,
  but input is never forwarded: the backbone streams terminals, it does not
  let remote clients type into them.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from weakref import WeakValueDictionary

import socketio

from agent_backbone.api.auth import credential_registration, credential_role
from agent_backbone.api.profile_binding import (
    BINDING_MISMATCH,
    PROFILE_FIELDS,
    binding_error,
    provider_current,
)
from agent_backbone.api.session_updates import SESSIONS_NAMESPACE
from agent_backbone.services.database import ProfileProvider
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
    sio = RegistrationAwareServer(
        async_mode="asgi",
        cors_allowed_origins=cors_origins or None,
        max_http_buffer_size=1_048_576,
        # Explicit loggers prevent the libraries installing unfiltered stderr handlers.
        logger=logging.getLogger("socketio.server"),
        engineio_logger=logging.getLogger("engineio.server"),
    )
    sio.register_namespace(SessionsNamespace(SESSIONS_NAMESPACE))
    sio.register_namespace(TerminalNamespace("/terminal"))
    return sio


async def _restore_dynamic_window_size(session_name: str) -> None:
    """Return tmux size selection to active-client control after a browser resize."""
    if not await set_window_size_mode(session_name, "latest"):
        log.error("Failed to restore tmux window-size latest for '%s'", session_name)


class RegistrationAwareServer(socketio.AsyncServer):
    """Retire scoped connections before they receive another broadcast or stream."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._profile_emissions: set[asyncio.Task] = set()

    def _emission_done(self, task: asyncio.Task) -> None:
        self._profile_emissions.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.warning("Profile socket emission failed (%s)", type(error).__name__)

    async def emit(
        self,
        event,
        data=None,
        to=None,
        room=None,
        skip_sid=None,
        namespace=None,
        callback=None,
        ignore_queue=False,
    ):
        app = getattr(self, "fastapi_app", None)
        provider = getattr(getattr(app, "state", None), "profile_provider", None)
        emission = self._emit(
            provider, event, data, to, room, skip_sid, namespace, callback, ignore_queue
        )
        if provider is None:
            return await emission
        # Own the fence in this task, independently of the caller. Packet sends
        # can trigger disconnect cleanup that cancels and awaits the caller;
        # making caller cancellation wait here would create a circular wait.
        task = asyncio.create_task(emission)
        self._profile_emissions.add(task)
        task.add_done_callback(self._emission_done)
        return await asyncio.shield(task)

    async def _emit(
        self,
        provider: ProfileProvider | None,
        event,
        data=None,
        to=None,
        room=None,
        skip_sid=None,
        namespace=None,
        callback=None,
        ignore_queue=False,
    ):
        async with AsyncExitStack() as operations:
            ns = self.namespace_handlers.get(namespace or "/")
            excluded = [skip_sid] if isinstance(skip_sid, str) else list(skip_sid or [])
            if isinstance(ns, _AuthenticatedNamespace):
                recipients = tuple(
                    sid
                    for sid, _ in self.manager.get_participants(ns.namespace, to or room)
                    if sid not in excluded and sid in ns._profiles
                )
                if not recipients:
                    return
                app = getattr(self, "fastapi_app", None)
                admitted_operation = True
                if provider is not None:
                    try:
                        await operations.enter_async_context(provider.operation())
                    except (ValueError, OSError):
                        admitted_operation = False
                # Reuse only within this emission; each SID still checks its captured
                # context and registration, and the next emission reconciles again.
                provider_check = (
                    provider,
                    admitted_operation
                    and (provider is None or await provider_current(provider, app)),
                )
                admitted = []
                for sid in recipients:
                    if sid in ns._profiles and await ns.registration_current(
                        sid, provider_check=provider_check
                    ):
                        admitted.append(sid)
                # Socket.IO joins the room before on_connect finishes. Revalidation
                # yields too: clients joining during it were never checked. Target
                # only admitted SIDs, never re-enumerate the original broadcast room.
                if not admitted:
                    return
                to, room = admitted, None
            return await super().emit(
                event,
                data,
                to=to,
                room=room,
                skip_sid=excluded or None,
                namespace=namespace,
                callback=callback,
                ignore_queue=ignore_queue,
            )


class _AuthenticatedNamespace(socketio.AsyncNamespace):
    """Namespace base that validates ``auth["api_key"]`` against the app config."""

    def __init__(self, namespace: str) -> None:
        super().__init__(namespace)
        self._registrations: dict[str, str] = {}
        self._profiles: dict = {}
        self._connecting: dict[str, object] = {}

    async def registration_current(
        self, sid: str, *, provider_check: tuple[ProfileProvider | None, bool] | None = None
    ) -> bool:
        """Recheck this connection, optionally sharing this emission's provider result."""
        app = getattr(self.server, "fastapi_app", None)
        provider = getattr(getattr(app, "state", None), "profile_provider", None)
        captured = self._profiles.get(sid)
        if provider_check is None:
            provider_check = (provider, provider is None or await provider_current(provider, app))
        if captured is not provider or provider_check[0] is not provider or not provider_check[1]:
            await self.disconnect(sid)
            return False
        registration = self._registrations.get(sid)
        if registration is None:  # operator and legacy shared credentials retain their authority
            return True
        app = getattr(self.server, "fastapi_app", None)
        db = getattr(getattr(app, "state", None), "db", None)
        try:
            if db is not None and await db.agents.registration_name(registration):
                return True
        except Exception as exc:
            log.warning(
                "Scoped socket registration check failed (%s); disconnecting", type(exc).__name__
            )
        await self.disconnect(sid)
        return False

    async def trigger_event(self, event, *args):
        if event == "connect":
            # AsyncNamespace converts cancellation to None, which Socket.IO
            # treats as accepted. Refuse instead so it also removes the pending
            # namespace membership before completing the handshake.
            try:
                return await self.on_connect(*args)
            except asyncio.CancelledError:
                return False
            except Exception:
                # Socket.IO only handles explicit refusals. Retire provisional
                # membership on other setup failures, then preserve the error.
                await self.server.manager.disconnect(args[0], self.namespace)
                raise
        if event == "disconnect" or not args:
            return await super().trigger_event(event, *args)
        sid = args[0]
        if sid not in self._profiles:
            return None
        provider = self._profiles[sid]
        async with AsyncExitStack() as operations:
            if provider is not None:
                try:
                    await operations.enter_async_context(provider.operation())
                except (ValueError, OSError):
                    await self.disconnect(sid)
                    return None
            if not await self.registration_current(sid):
                return None
            return await super().trigger_event(event, *args)

    async def on_disconnect(self, sid: str) -> None:
        self._connecting.pop(sid, None)
        self._registrations.pop(sid, None)
        self._profiles.pop(sid, None)

    def _config(self):
        app = getattr(self.server, "fastapi_app", None)
        return getattr(getattr(app, "state", None), "config", None)

    async def on_connect(self, sid: str, environ: dict, auth: dict | None = None) -> bool:
        app = getattr(self.server, "fastapi_app", None)
        state = getattr(app, "state", None)
        provider = getattr(state, "profile_provider", None)
        config = getattr(state, "config", None)
        db = getattr(state, "db", None)
        feed = getattr(state, "feed", None)
        raw = auth.get("api_key") if isinstance(auth, dict) else None
        candidate = raw if isinstance(raw, str) else None
        if config is None or (
            credential_role(candidate, config.api_key) is None
            if config.api_key
            else not config.security.allow_unauthenticated
        ):
            log.warning(
                "Socket.IO %s connection rejected — invalid auth (sid=%s)", self.namespace, sid
            )
            return False

        admission = object()
        self._connecting[sid] = admission
        accepted = False

        def current() -> bool:
            # Do not recapture a provider or its content after any admission await.
            return (
                self._connecting.get(sid) is admission
                and getattr(self.server, "fastapi_app", None) is app
                and getattr(state, "profile_provider", None) is provider
                and getattr(state, "config", None) is config
                and getattr(state, "db", None) is db
                and getattr(state, "feed", None) is feed
            )

        try:
            async with AsyncExitStack() as operations:
                if provider is not None:
                    try:
                        await operations.enter_async_context(provider.operation())
                    except (ValueError, OSError):
                        raise socketio.exceptions.ConnectionRefusedError(
                            BINDING_MISMATCH["message"], {"code": BINDING_MISMATCH["code"]}
                        ) from None
                if not current():
                    return False
                registration = credential_registration(candidate, config.api_key)
                if registration and not (db and await db.agents.registration_name(registration)):
                    return False
                if not current():
                    return False
                values = {
                    field: auth[field]
                    for field in PROFILE_FIELDS
                    if isinstance(auth, dict) and field in auth
                }
                if error := await binding_error(provider, values, app):
                    raise socketio.exceptions.ConnectionRefusedError(
                        error["message"], {"code": error["code"]}
                    )
                if not current():
                    return False
                self._profiles[sid] = provider
                if registration:
                    self._registrations[sid] = registration
                await self._connected(sid, feed, current)
            accepted = current() and sid in self._profiles
            return accepted
        finally:
            if self._connecting.get(sid) is admission:
                self._connecting.pop(sid)
            if not accepted:
                self._registrations.pop(sid, None)
                self._profiles.pop(sid, None)

    async def _connected(self, sid: str, feed, current) -> None:
        """Finish namespace setup while the captured admission fence is held."""


class SessionsNamespace(_AuthenticatedNamespace):
    """Socket.IO namespace for enriched session state subscriptions.

    Updates are change-driven; a full snapshot is sent to each client on
    connect so subscribers never start blind.
    """

    async def _connected(self, sid: str, feed, current) -> None:
        from agent_backbone.api.session_updates import SESSIONS_UPDATE_EVENT

        if feed is None:
            return
        try:
            payload = [agent.model_dump(mode="json") for agent in await feed.snapshot()]
            if current():
                await self.emit(SESSIONS_UPDATE_EVENT, payload, to=sid)
        except Exception:
            log.exception("Could not send the initial /sessions snapshot (non-fatal)")


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
        self._background: set[asyncio.Task] = set()
        self._resource_tasks: dict[tuple[str, str], set[asyncio.Task]] = {}
        self._resource_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._resources: dict[tuple[str, str], tuple[PtyManager, AsyncExitStack]] = {}

    async def _resource_operation(self, sid: str, session_name: str, action):
        """Own pending work; attaches return a forwarder to retire on cancellation."""
        key = (sid, session_name)
        lock = self._resource_locks.setdefault(key, asyncio.Lock())
        abandoned = asyncio.Event()
        origin = asyncio.current_task()

        async def run():
            async with lock:
                if origin.cancelling():
                    abandoned.set()
                return await action(abandoned, origin)

        task = asyncio.create_task(run())
        pending = self._resource_tasks.setdefault(key, set())
        pending.add(task)

        def finished(done):
            pending.discard(done)
            if not pending:
                self._resource_tasks.pop(key, None)
            if not done.cancelled() and (error := done.exception()) is not None:
                log.warning("Terminal resource operation failed (%s)", type(error).__name__)

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not cancel a worker or wait on it here: a packet send may be
            # disconnecting this very caller. The worker retires an abandoned attach.
            abandoned.set()

            def retire_attachment(done):
                # An attach can finish before its cancelled caller resumes.
                # Its returned forwarder owns a retirement callback, even then.
                if not done.cancelled() and done.exception() is None:
                    forwarding = done.result()
                    if forwarding is not None:
                        forwarding.cancel()

            task.add_done_callback(retire_attachment)
            raise

    async def _attach_subscription_client(
        self, sid: str, session_name: str, cols: int, rows: int, *, join_room: bool = False
    ) -> None:
        async def attach(abandoned, origin):
            if sid not in self._profiles or abandoned.is_set():
                return
            try:
                if join_room:
                    await self.enter_room(sid, f"session:{session_name}")
                return await self._attach_locked(sid, session_name, cols, rows, abandoned, origin)
            except Exception:
                if join_room:
                    # Retire this failed join before another worker can replace it.
                    await self._cleanup_locked(sid, session_name, origin)
                raise

        await self._resource_operation(sid, session_name, attach)

    async def _attach_locked(self, sid, session_name, cols, rows, abandoned, origin):
        if sid not in self._profiles or abandoned.is_set():
            return
        provider = self._profiles[sid]
        key = (sid, session_name)
        await self._detach_locked(sid, session_name, origin)
        if sid not in self._profiles or self._profiles[sid] is not provider or abandoned.is_set():
            return
        fence = AsyncExitStack()
        if provider is not None:
            await fence.enter_async_context(provider.operation())
        mgr = get_pty_manager()
        self._resources[key] = (mgr, fence)
        self._subscriptions.setdefault(sid, {})[session_name] = None
        try:
            if (
                sid not in self._profiles
                or self._profiles[sid] is not provider
                or abandoned.is_set()
            ):
                return
            pty_session = await mgr.create(sid, session_name, cols, rows)
            if (
                sid not in self._profiles
                or self._profiles[sid] is not provider
                or abandoned.is_set()
            ):
                return

            pty_session.on_data_dropped = lambda sn: self._on_data_dropped(sid, sn)
            task = asyncio.create_task(
                self._forward_pty_output(sid, session_name, pty_session.output_queue),
                name=f"sio-pty-{sid}-{session_name}",
            )
            self._subscriptions[sid][session_name] = task

            def ended(done):
                # EOF or a cancelled forwarder is not proof of process exit.
                cleanup = asyncio.create_task(self._retire_forwarder(sid, session_name, done))
                self._background.add(cleanup)
                cleanup.add_done_callback(self._background.discard)

            task.add_done_callback(ended)
            return task
        finally:
            if self._subscriptions.get(sid, {}).get(session_name) is None:
                await self._detach_locked(sid, session_name, origin)

    async def _retire_forwarder(self, sid, session_name, forwarding):
        async def retire(abandoned, origin):
            if self._subscriptions.get(sid, {}).get(session_name) is forwarding:
                await self._detach_locked(sid, session_name, origin)

        try:
            await self._resource_operation(sid, session_name, retire)
        except Exception:
            # Keep the subscription and fence available for a later cleanup retry.
            log.exception("Could not retire ended terminal attachment")

    async def _detach_locked(self, sid, session_name, origin):
        key = (sid, session_name)
        sid_subs = self._subscriptions.get(sid, {})
        task = sid_subs.get(session_name)
        if task is None and key not in self._resources:
            return
        if isinstance(task, asyncio.Task) and task is not origin:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.warning("PTY forwarding task failed during detach: %s", e)
        await self._manager_for(sid, session_name).remove(sid, session_name)
        resource = self._resources.get(key)
        if resource is not None:
            await resource[1].aclose()
            del self._resources[key]
        sid_subs[session_name] = None

    def _manager_for(self, sid: str, session_name: str) -> PtyManager:
        resource = self._resources.get((sid, session_name))
        return resource[0] if resource is not None else get_pty_manager()

    async def _detach_subscription_client(self, sid: str, session_name: str) -> None:
        async def detach(abandoned, origin):
            await self._detach_locked(sid, session_name, origin)

        await self._resource_operation(sid, session_name, detach)

    async def on_join(self, sid: str, data: dict) -> None:
        session_name = data.get("session", "") if isinstance(data, dict) else ""
        if not isinstance(session_name, str) or not session_name:
            await self.emit("error", {"message": "Missing session name"}, to=sid)
            return

        # Only registered agents are streamed — never an arbitrary tmux
        # session of the same user (same rule as GET /sessions/{name}/terminal).
        config = self._config()
        spec = config.agents.get(session_name) if config is not None else None
        if spec is None:
            await self.emit(
                "error", {"message": f"'{session_name}' is not a registered agent"}, to=sid
            )
            return
        if spec.inbox_only:
            await self.emit(
                "error", {"message": f"'{session_name}' is inbox-only: it has no terminal"}, to=sid
            )
            return
        if not await session_exists(session_name):
            await self.emit("error", {"message": f"Session '{session_name}' not found"}, to=sid)
            return

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

        if sid not in self._profiles:
            return
        await self._attach_subscription_client(sid, session_name, cols, rows, join_room=True)

        log.info("sid=%s joined session '%s' (read-only stream)", sid, session_name)

    def _session_of(self, data: dict | object) -> str:
        """The ``session`` in a client payload, or "" when it is malformed.

        Socket.IO payloads are client input: ``null``, a string or a list
        must not reach ``dict.get`` (``AttributeError``).
        """
        if not isinstance(data, dict):
            return ""
        session = data.get("session", "")
        return session if isinstance(session, str) else ""

    async def on_leave(self, sid: str, data: dict) -> None:
        await self._cleanup_session(sid, self._session_of(data))

    async def on_resize(self, sid: str, data: dict) -> None:
        if not isinstance(data, dict):
            return
        session_name = self._session_of(data)
        cols = data.get("cols")
        rows = data.get("rows")
        if not session_name or not cols or not rows:
            return

        if (
            session_name not in self._subscriptions.get(sid, {})
            and (sid, session_name) not in self._resource_tasks
        ):
            await self.emit("error", {"message": f"Not joined to session '{session_name}'"}, to=sid)
            return

        try:
            clamped_cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
            clamped_rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))
        except (ValueError, TypeError):
            return

        async def resize(abandoned, origin):
            if sid not in self._profiles or session_name not in self._subscriptions.get(sid, {}):
                return
            pty_session = self._manager_for(sid, session_name).get(sid, session_name)
            if pty_session is None:
                return await self._attach_locked(
                    sid, session_name, clamped_cols, clamped_rows, abandoned, origin
                )
            pty_session.resize(clamped_cols, clamped_rows)
            if not await resize_window(session_name, clamped_cols, clamped_rows):
                log.error("resize_window failed for '%s'", session_name)
            await _restore_dynamic_window_size(session_name)

        await self._resource_operation(sid, session_name, resize)

    async def on_release_dims(self, sid: str, data: dict) -> None:
        session_name = self._session_of(data)
        if not session_name:
            return
        await self._detach_subscription_client(sid, session_name)

    async def on_pause(self, sid: str, data: dict) -> None:
        session_name = self._session_of(data)
        pty_session = self._manager_for(sid, session_name).get(sid, session_name)
        if pty_session:
            pty_session.pause()

    async def on_resume(self, sid: str, data: dict) -> None:
        session_name = self._session_of(data)
        pty_session = self._manager_for(sid, session_name).get(sid, session_name)
        if pty_session:
            pty_session.resume()

    async def on_disconnect(self, sid: str) -> None:
        await super().on_disconnect(sid)
        sessions = set(self._subscriptions.get(sid, {})) | {
            name for owner, name in self._resource_tasks if owner == sid
        }
        for session_name in sessions:
            try:
                await self._cleanup_session(sid, session_name)
            except Exception:
                # Let Socket.IO finish removing the client from its rooms even
                # if a PTY fails to detach, and still clean up other sessions.
                log.exception("Could not clean up disconnected terminal subscription")
        log.info("sid=%s disconnected, attempted cleanup of %d sessions", sid, len(sessions))

    async def _cleanup_session(self, sid: str, session_name: str) -> None:
        async def cleanup(abandoned, origin):
            if (
                session_name not in self._subscriptions.get(sid, {})
                and (sid, session_name) not in self._resources
            ):
                return
            await self._cleanup_locked(sid, session_name, origin)

        await self._resource_operation(sid, session_name, cleanup)

    async def _cleanup_locked(self, sid, session_name, origin):
        await self._detach_locked(sid, session_name, origin)
        sid_subs = self._subscriptions.get(sid, {})
        sid_subs.pop(session_name, None)
        if not sid_subs:
            self._subscriptions.pop(sid, None)
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
            # Socket.IO can swallow cancellation during inline disconnect.
            # Do not resume an idle read after a detach has cancelled this task.
            while not asyncio.current_task().cancelling():
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
