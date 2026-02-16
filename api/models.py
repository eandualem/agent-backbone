"""Pydantic response models for the REST API."""

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

    session: str
    entity: str
    display_name: str = ""
    role: str = ""
    state: str = "unknown"
    current_issue: int | None = None
    online: bool = False
    plan_file: str | None = None
    plan_title: str | None = None
    tmux_created: str | None = None
    tmux_attached: bool = False
    tmux_windows: int = 0


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
    labels: ParsedLabelsResponse = Field(default_factory=ParsedLabelsResponse)
    priority_score: float = 0.0


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


# --- Status ---


class ServiceHealth(BaseModel):
    """Health check for backbone services."""

    gateway: str = "up"
    prefect_server: str = "unknown"
    database: str = "unknown"


class SystemDigest(BaseModel):
    """System-wide status digest."""

    active_sessions: list[str] = Field(default_factory=list)
    agent_count: int = 0
    pending_issues: int = 0
    failed_deliveries: int = 0
    agents: list[EnrichedAgent] = Field(default_factory=list)


# --- Heartbeats ---


class HeartbeatRecord(BaseModel):
    """A single heartbeat delivery record."""

    id: int
    agent: str
    delivered_at: str
    outcome: str
    message: str | None = None


# --- Workflows ---


class WorkflowInfo(BaseModel):
    """Workflow template metadata."""

    name: str
    description: str = ""
    module: str = ""


# --- Prefect ---


class FlowRunResponse(BaseModel):
    """Prefect flow run summary."""

    id: str
    name: str = ""
    flow_id: str = ""
    state: str = "unknown"  # completed/failed/running/pending
    start_time: str | None = None
    duration: float | None = None  # seconds
    detail: str = ""


# --- Actions ---


class ActionRecord(BaseModel):
    """Agent action log entry."""

    ts: float
    session: str
    action: str
    issue: int | None = None


# --- Files ---


class FileNode(BaseModel):
    """File or directory entry."""

    name: str
    type: str  # "file" or "directory"
    path: str
    children: list[FileNode] | None = None
