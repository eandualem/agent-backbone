"""Fire-and-forget run event broadcasting on the /runs namespace."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from agent_backbone.services._locator import get_sio

if TYPE_CHECKING:
    import socketio

log = logging.getLogger(__name__)

RUN_EVENT = "run:event"
RUNS_NAMESPACE = "/runs"


async def emit_run_event(
    event_type: str,
    *,
    context: dict[str, Any] | None = None,
    source: str = "backbone",
    data: dict[str, Any] | None = None,
    sio: socketio.AsyncServer | None = None,
) -> None:
    """Emit a run event to all connected Socket.IO clients.

    Fire-and-forget: never raises, never blocks callers. All exceptions are
    caught and logged as warnings.
    """
    try:
        server = sio if sio is not None else get_sio()
        if server is None:
            log.warning(
                "[RUN] No Socket.IO server available — skipping event %s",
                event_type,
            )
            return

        payload: dict[str, Any] = {
            "type": event_type,
            "context": context or {},
            "source": source,
            "timestamp": int(time.time()),
            "data": data or {},
        }

        # RDS-86A: Route to run room if runId is present, and always broadcast.
        run_id = (context or {}).get("runId")
        if run_id:
            await server.emit(
                RUN_EVENT,
                payload,
                namespace=RUNS_NAMESPACE,
                room=f"run:{run_id}",
            )
        await server.emit(
            RUN_EVENT,
            payload,
            namespace=RUNS_NAMESPACE,
        )
        log.info("[RUN] Emitted %s from %s context=%s", event_type, source, context)
    except Exception:
        log.warning("[RUN] Failed to emit event %s", event_type, exc_info=True)
