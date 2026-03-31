"""Activity API models."""

from __future__ import annotations

from pydantic import BaseModel


class ActivityEvent(BaseModel):
    """Unified activity timeline event."""

    ts: float
    type: str  # "action" | "delivery" | "heartbeat"
    entity: str
    summary: str
