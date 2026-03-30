"""Activity event persistence helpers for the /activity snapshot stream."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from agent_backbone.api.data_streams import notify_stream
from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)


async def record_activity_event(
    db: BackboneDB,
    *,
    session: str,
    event_type: str,
    data: dict[str, Any] | None = None,
    entity: str | None = None,
    source_ref: str | None = None,
    ts: float | None = None,
) -> None:
    """Persist an activity event and refresh the /activity snapshot stream."""
    try:
        payload = json.dumps(data) if data else None
        timestamp = ts if ts is not None else time.time()
        await db.record_activity(
            session,
            event_type,
            payload,
            str(timestamp),
            entity=entity,
            source_ref=source_ref,
        )
        await notify_stream("activity")
    except Exception:
        log.warning("Failed to record activity event %s", event_type, exc_info=True)
