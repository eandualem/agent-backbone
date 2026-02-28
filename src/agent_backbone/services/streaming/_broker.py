"""Fan-out broker for SSE streaming of tmux control mode events.

One ControlModeConnection producer per session, multiple SSE client consumers.
Grace-period disconnect when all clients leave to avoid expensive subprocess
teardown/reconnection on page refreshes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field

from agent_backbone.config import BackboneConfig
from agent_backbone.services.streaming._control_mode import (
    ControlModeConnection,
    ControlModeManager,
    _manager,
)
from agent_backbone.services.streaming.models import (
    ControlModeEvent,
    ExitEvent,
    LayoutChangeEvent,
    ModeChangeEvent,
    OutputEvent,
    SessionEvent,
    WindowEvent,
)

log = logging.getLogger(__name__)

# Max queued SSE frames per client before dropping oldest
_MAX_QUEUE_SIZE = 256


def _event_type_name(event: ControlModeEvent) -> str:
    """Map a ControlModeEvent to its SSE event type string."""
    match event:
        case OutputEvent():
            return "output"
        case ModeChangeEvent():
            return "mode_change"
        case LayoutChangeEvent():
            return "layout_change"
        case WindowEvent():
            return "window"
        case SessionEvent():
            return "session"
        case ExitEvent():
            return "exit"
        case _:
            return "unknown"


def _event_to_data(event: ControlModeEvent) -> str:
    """Serialize a ControlModeEvent to a JSON string."""
    return json.dumps(asdict(event))


def format_sse(event_type: str, data: str) -> str:
    """Format an SSE frame: event + data lines + blank line terminator."""
    return f"event: {event_type}\ndata: {data}\n\n"


def parse_sse_frame(frame: str) -> tuple[str, str] | None:
    """Parse an SSE frame into (event_type, data_string).

    Expects the format produced by format_sse():
        event: <type>\\ndata: <json>\\n\\n

    Returns None for unparseable frames.
    """
    event_type = None
    data = None
    for line in frame.strip().splitlines():
        if line.startswith("event: "):
            event_type = line[7:]
        elif line.startswith("data: "):
            data = line[6:]
    if event_type is not None and data is not None:
        return event_type, data
    return None


@dataclass
class SessionBroadcast:
    """Per-session state for the broker."""

    connection: ControlModeConnection
    producer_task: asyncio.Task | None = None
    clients: dict[int, asyncio.Queue[str | None]] = field(default_factory=dict)
    grace_task: asyncio.Task | None = None
    next_client_id: int = 0


class StreamBroker:
    """Manages fan-out from ControlModeConnection to SSE clients.

    Thread-safe via asyncio.Lock. One producer task per session consumes
    the connection stream and distributes events to all client queues.
    """

    def __init__(self, config: BackboneConfig, manager: ControlModeManager | None = None) -> None:
        self._config = config
        self._manager = manager or _manager
        self._sessions: dict[str, SessionBroadcast] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, session_name: str) -> tuple[int, asyncio.Queue[str | None]]:
        """Subscribe a new SSE client to a session's event stream.

        Creates the connection and producer task on first client.
        Cancels any pending grace-period teardown.

        Returns (client_id, queue) or raises RuntimeError on connection failure.
        """
        async with self._lock:
            broadcast = self._sessions.get(session_name)

            if broadcast is not None and broadcast.grace_task is not None:
                broadcast.grace_task.cancel()
                broadcast.grace_task = None
                log.info("Grace period cancelled for '%s' — new client", session_name)

            needs_producer = (
                broadcast is None
                or broadcast.producer_task is None
                or broadcast.producer_task.done()
            )
            if needs_producer:
                conn = await self._manager.connect(session_name, self._config.control_mode)
                broadcast = SessionBroadcast(connection=conn)
                self._sessions[session_name] = broadcast
                broadcast.producer_task = asyncio.create_task(
                    self._produce(session_name, broadcast),
                    name=f"stream-producer-{session_name}",
                )

            client_id = broadcast.next_client_id
            broadcast.next_client_id += 1
            queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
            broadcast.clients[client_id] = queue
            log.info(
                "Client %d subscribed to '%s' (%d clients)",
                client_id,
                session_name,
                len(broadcast.clients),
            )
            return client_id, queue

    async def unsubscribe(self, session_name: str, client_id: int) -> None:
        """Remove a client. Starts grace-period teardown if last client."""
        async with self._lock:
            broadcast = self._sessions.get(session_name)
            if broadcast is None:
                return

            broadcast.clients.pop(client_id, None)
            log.info(
                "Client %d unsubscribed from '%s' (%d clients remaining)",
                client_id,
                session_name,
                len(broadcast.clients),
            )

            if not broadcast.clients and broadcast.grace_task is None:
                grace = self._config.control_mode.stream_grace_period_seconds
                broadcast.grace_task = asyncio.create_task(
                    self._grace_disconnect(session_name, grace),
                    name=f"stream-grace-{session_name}",
                )

    async def _produce(self, session_name: str, broadcast: SessionBroadcast) -> None:
        """Background task: consume connection stream, fan out to all client queues."""
        try:
            async for event in broadcast.connection.stream():
                event_type = _event_type_name(event)
                data = _event_to_data(event)
                frame = format_sse(event_type, data)

                for cid, queue in list(broadcast.clients.items()):
                    try:
                        queue.put_nowait(frame)
                    except asyncio.QueueFull:
                        # Drop oldest event for slow clients
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        try:
                            queue.put_nowait(frame)
                        except asyncio.QueueFull:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Producer error for '%s'", session_name)
        finally:
            # Stream ended — send error event + sentinel to all clients
            error_frame = format_sse("error", json.dumps({"message": "stream ended"}))
            for queue in broadcast.clients.values():
                try:
                    queue.put_nowait(error_frame)
                except asyncio.QueueFull:
                    pass
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def _grace_disconnect(self, session_name: str, delay: float) -> None:
        """Wait, then tear down the session if still no clients."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        async with self._lock:
            broadcast = self._sessions.get(session_name)
            if broadcast is None or broadcast.clients:
                return  # Clients arrived during grace period
            await self._teardown(session_name, broadcast)

    async def _teardown(self, session_name: str, broadcast: SessionBroadcast) -> None:
        """Cancel producer, signal queues, disconnect manager. Caller holds lock."""
        if broadcast.producer_task and not broadcast.producer_task.done():
            broadcast.producer_task.cancel()
            try:
                await broadcast.producer_task
            except asyncio.CancelledError:
                pass

        if broadcast.grace_task and not broadcast.grace_task.done():
            broadcast.grace_task.cancel()

        # Signal remaining clients
        for queue in broadcast.clients.values():
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        await self._manager.disconnect(session_name)
        self._sessions.pop(session_name, None)
        log.info("Teardown complete for '%s'", session_name)

    async def shutdown(self) -> None:
        """Tear down all sessions. Called from app lifespan."""
        async with self._lock:
            for session_name in list(self._sessions):
                broadcast = self._sessions[session_name]
                await self._teardown(session_name, broadcast)

    def client_count(self, session_name: str) -> int:
        """Number of active SSE clients for a session."""
        broadcast = self._sessions.get(session_name)
        return len(broadcast.clients) if broadcast else 0
