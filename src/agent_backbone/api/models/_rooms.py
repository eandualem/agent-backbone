"""Room-related API models."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator


class RoomMessage(BaseModel):
    """A single message in a room transcript."""

    id: str
    sender: str
    recipients: list[str]
    mode: str  # "directed" | "broadcast" | "response"
    content: str
    timestamp: str  # ISO 8601

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_timestamp(cls, v: object) -> str:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, tz=UTC).isoformat()
        return str(v)


class Room(BaseModel):
    """Meeting room with participants and transcript."""

    id: str
    title: str
    description: str = ""
    moderator: str
    participants: list[str]
    state: str = "active"
    transcript: list[RoomMessage] = Field(default_factory=list)
    cursors: dict[str, int] = Field(default_factory=dict)
    created_at: str = ""  # ISO 8601
    updated_at: str = ""  # ISO 8601

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _coerce_timestamps(cls, v: object) -> str:
        if isinstance(v, (int, float)):
            if v == 0.0:
                return ""
            return datetime.fromtimestamp(v, tz=UTC).isoformat()
        return str(v)


class RoomCreate(BaseModel):
    """Request body for creating a room."""

    title: str
    description: str = ""
    moderator: str
    participants: list[str]


class DirectedMessageRequest(BaseModel):
    """Request body for directed message."""

    target: str
    content: str
    sender: str = ""


class BroadcastMessageRequest(BaseModel):
    """Request body for broadcast message."""

    content: str


class ResponseMessageRequest(BaseModel):
    """Request body for participant response."""

    sender: str
    content: str


class RoomStateUpdate(BaseModel):
    """Request body for updating room state."""

    state: str
