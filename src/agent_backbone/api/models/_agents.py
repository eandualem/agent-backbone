"""Agent-related API models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnrichedAgent(BaseModel):
    """Agent with merged static config + live state."""

    session: str
    entity: str
    display_name: str = ""
    role: str = ""
    figure: str = ""
    org: str = ""
    groups: list[str] = Field(default_factory=list)
    home: str = ""
    type: str = "coding_agent"  # "named_entity" | "coding_agent"
    entity_type: str = "agent"  # "agent" | "service"
    state: str = "offline"
    current_issue: int | None = None
    context: dict[str, Any] | str | None = None
    online: bool = False
    plan_file: str | None = None
    plan_title: str | None = None
    tmux_created: str | None = None
    tmux_attached: bool = False
    tmux_windows: int = 0
    last_activity: float | None = None
    state_since: float | None = None
    runtime: str | None = None


class AgentStartRequest(BaseModel):
    """Request body for starting an agent session."""

    runtime: str = "claude"
    model: str | None = None
    resume: bool = False
    working_directory: str | None = None


class AgentStartResponse(BaseModel):
    """Response for agent start endpoint."""

    ok: bool
    session: str
    working_directory: str | None = None
    runtime: str = "claude"
    model: str | None = None
    already_existed: bool = False


class AgentStopResponse(BaseModel):
    """Response for agent stop endpoint."""

    ok: bool
    session: str


class AgentStateDetail(BaseModel):
    """Detailed agent state snapshot."""

    session: str
    state: str = "offline"
    current_issue: int | None = None
    timestamp: float = 0.0
    source: str = "default"
    started_at: float | None = None
    context: dict[str, Any] | str | None = None
    plan_file: str | None = None
    plan_title: str | None = None


class StateUpdateRequest(BaseModel):
    """Request body for updating agent state via API."""

    entity: str = ""
    state: str
    issue: int | None = None
    context: dict[str, Any] | str | None = None
    ts: float = 0.0
    plan_file: str | None = None
    plan_title: str | None = None
    hook_event: str = ""
    cli: str = ""


class ActivityCreateRequest(BaseModel):
    """Request body for recording an agent activity event."""

    event: str
    ts: float

    model_config = ConfigDict(extra="allow")


class AgentActivityEvent(BaseModel):
    """Agent activity event from the database."""

    id: int
    session: str
    event: str
    data: dict | None = None
    ts: float
    received_at: str
