"""Pydantic request/response models for the REST API."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ListEnvelope(BaseModel, Generic[T]):
    """Generic list wrapper with total count."""

    items: list[T] = Field(default_factory=list)
    total: int = 0


class ErrorDetail(BaseModel):
    """Standard error response."""

    error: str
    detail: str = ""


# --- Agents ---


class EnrichedAgent(BaseModel):
    """Agent with merged static config + live state."""

    name: str
    session: str
    configured: bool = True
    runtime: str | None = None
    """Runtime the session was launched with (live), else the configured runtime."""
    model: str | None = None
    dir: str = ""
    repo: str = ""
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    state: str = "unknown"
    current_issue: int | None = None
    online: bool = False
    plan_file: str | None = None
    plan_title: str | None = None
    tmux_created: str | None = None
    tmux_attached: bool = False
    tmux_windows: int = 0
    last_activity: float | None = None
    state_since: float | None = None


class AgentStartRequest(BaseModel):
    """Request body for starting an agent session."""

    runtime: str | None = None
    model: str | None = None
    resume: bool = False
    working_directory: str | None = None
    """Required for sessions that are not configured agents."""


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
    state: str = "unknown"
    current_issue: int | None = None
    timestamp: float = 0.0
    source: str = "default"
    started_at: float | None = None
    plan_file: str | None = None
    plan_title: str | None = None


class StateUpdateRequest(BaseModel):
    """Request body for updating agent state via API (used by runtime hooks)."""

    state: str
    issue: int | None = None
    entity: str = ""
    context: str = ""
    ts: float = 0.0
    plan_file: str | None = None
    plan_title: str | None = None


class RuntimeInfo(BaseModel):
    """Runtime option for agent sessions."""

    id: str
    display_name: str
    available: bool = True


# --- Issues ---


class ParsedLabelsResponse(BaseModel):
    """Label breakdown for an issue."""

    sender: str = "unknown"
    targets: list[str] = Field(default_factory=list)
    issue_type: str = ""
    priority: str = ""


class IssueResponse(BaseModel):
    """Issue with parsed labels and priority score."""

    number: int
    title: str = ""
    state: str = "open"
    html_url: str = ""
    repo_full_name: str = ""
    labels: ParsedLabelsResponse = Field(default_factory=ParsedLabelsResponse)
    priority_score: float = 0.0


class IssueCreateRequest(BaseModel):
    """Request body for creating an issue."""

    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    repo: str | None = None
    """``owner/name``; defaults to the configured coordination repo."""


class IssueCommentRequest(BaseModel):
    body: str


class IssueUpdateRequest(BaseModel):
    state: str


class IssueCommentResponse(BaseModel):
    """Issue comment with parsed from-tag."""

    id: int = 0
    body: str = ""
    user_login: str = "unknown"
    from_entity: str | None = None


class IssueDependencies(BaseModel):
    """Sub-issues and parent issues for an issue."""

    sub_issues: list[IssueResponse] = Field(default_factory=list)
    parents: list[int] = Field(default_factory=list)


# --- Deliveries ---


class DeliveryRecord(BaseModel):
    """A single delivery attempt record."""

    id: int
    issue_number: int
    target_entity: str
    session_name: str
    outcome: str
    flow_name: str = ""
    created_at: str = ""


class DeliveryStats(BaseModel):
    """Aggregated delivery statistics."""

    total: int = 0
    delivered: int = 0
    failed: int = 0
    deferred: int = 0
    offline: int = 0


# --- Plans ---


class PlanDetail(BaseModel):
    """Plan awaiting approval."""

    session: str
    state: str = "plan_waiting"
    plan_file: str | None = None
    plan_title: str | None = None
    content: str | None = None


class PlanRejectRequest(BaseModel):
    feedback: str


class PlanRespondRequest(BaseModel):
    input: str


# --- Messages ---


class MessageRequest(BaseModel):
    """Request body for message delivery to an agent."""

    target_session: str
    from_entity: str
    message: str
    priority: bool = False


class MessageResponse(BaseModel):
    ok: bool
    session: str
    outcome: str


class TelegramReplyRequest(BaseModel):
    """Request body for routing an agent reply to its Telegram topic."""

    session: str
    text: str


# --- Status ---


class JobStatusResponse(BaseModel):
    name: str
    interval_seconds: float
    runs: int = 0
    failures: int = 0
    running: bool = False
    last_started: float | None = None
    last_finished: float | None = None
    last_error: str | None = None


class ServiceHealth(BaseModel):
    """Health of the backbone's own components."""

    api: str = "up"
    database: str = "unknown"
    scheduler: str = "unknown"
    telegram: str = "disabled"
    github: str = "disabled"
    jobs: list[JobStatusResponse] = Field(default_factory=list)


class SystemDigest(BaseModel):
    """System-wide status digest."""

    active_sessions: list[str] = Field(default_factory=list)
    agent_count: int = 0
    pending_issues: int | None = 0
    failed_deliveries: int = 0
    agents: list[EnrichedAgent] = Field(default_factory=list)


class AgentConfigResponse(BaseModel):
    """Non-secret view of a configured agent."""

    name: str
    dir: str
    runtime: str
    model: str | None = None
    repo: str = ""
    tags: list[str] = Field(default_factory=list)
    description: str = ""
