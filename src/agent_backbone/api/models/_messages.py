"""Message-related API models."""

from __future__ import annotations

from pydantic import BaseModel


class MessageRequest(BaseModel):
    """Request body for inter-agent message delivery."""

    target_session: str
    from_entity: str
    message: str
    priority: bool = False


class MessageResponse(BaseModel):
    """Response from inter-agent message delivery."""

    ok: bool
    session: str
    outcome: str
