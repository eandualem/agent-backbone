"""Track and run API models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunActionRequest(BaseModel):
    """Request body for run action execution."""

    action_type: str
    params: dict[str, Any] = Field(default_factory=dict)
    track_context: dict[str, Any] = Field(default_factory=dict)


class RunActionResponse(BaseModel):
    """Response from run action execution."""

    ok: bool
    action_type: str
    result: dict[str, Any] = Field(default_factory=dict)


class TrackCreate(BaseModel):
    """Request body for creating a track."""

    id: str
    name: str
    description: str = ""
    definition: dict[str, Any] = Field(default_factory=dict)


class TrackUpdate(BaseModel):
    """Request body for updating a track."""

    name: str | None = None
    description: str | None = None
    definition: dict[str, Any] | None = None


class TrackResponse(BaseModel):
    """A track definition."""

    id: str
    name: str
    description: str
    definition: dict[str, Any]
    created_at: str
    updated_at: str


class RunCreate(BaseModel):
    """Request body for creating a track run."""

    id: str
    repo_full_name: str
    issue_number: int
    context: dict[str, Any] = Field(default_factory=dict)
    current_state: str
    status: str = "active"
    last_projected_state: str | None = None


class RunUpdate(BaseModel):
    """Request body for updating a track run."""

    repo_full_name: str | None = None
    issue_number: int | None = None
    current_state: str | None = None
    status: str | None = None
    last_projected_state: str | None = None
    context: dict[str, Any] | None = None
    history: list[dict[str, Any]] | None = None


class RunResponse(BaseModel):
    """A track run."""

    id: str
    track_id: str
    repo_full_name: str
    issue_number: int
    context: dict[str, Any]
    current_state: str
    status: str
    last_projected_state: str | None
    history: list[dict[str, Any]]
    created_at: str
    updated_at: str


class TrackLayoutRequest(BaseModel):
    """Request body for upserting track layout positions."""

    positions: dict[str, Any]


class TrackLayoutResponse(BaseModel):
    """Track graph layout positions."""

    track_id: str
    positions: dict[str, Any]
    updated_at: str
