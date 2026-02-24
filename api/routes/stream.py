"""SSE endpoint for live terminal streaming via tmux control mode."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from api.broker import get_or_create_broker
from api.deps import get_config
from src.config import BackboneConfig
from src.stream_broker import StreamBroker
from src.tmux import session_exists

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["stream"])


def get_broker(config: BackboneConfig = Depends(get_config)) -> StreamBroker:
    """Get or create the StreamBroker singleton."""
    return get_or_create_broker(config)


@router.get("/agents/{session}/stream")
async def stream_session(
    session: str,
    broker: StreamBroker = Depends(get_broker),
):
    """Stream live terminal output from a tmux session via SSE.

    Returns Server-Sent Events with types: output, mode_change,
    layout_change, window, session, exit, error.
    """
    if not await session_exists(session):
        raise HTTPException(status_code=404, detail=f"Session '{session}' not found")

    try:
        client_id, queue = await broker.subscribe(session)
    except Exception:
        log.exception("Failed to start control mode for '%s'", session)
        raise HTTPException(status_code=502, detail="Failed to start control mode connection")

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            while True:
                frame = await queue.get()
                if frame is None:
                    break
                yield frame
        finally:
            await broker.unsubscribe(session, client_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
