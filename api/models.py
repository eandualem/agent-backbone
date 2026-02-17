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
    type: str = "coding_agent"  # "named_entity" | "coding_agent"
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
    source: str = "prefect"
    last_run: str | None = None
    steps: list[dict] = Field(default_factory=list)


class WorkflowCreateRequest(BaseModel):
    """Request body for creating a JSON workflow."""

    name: str
    description: str = ""
    steps: list[dict]


class AgentStartRequest(BaseModel):
    """Optional request body for starting an agent session."""

    working_directory: str | None = None
    runtime: str | None = None


class RuntimeInfo(BaseModel):
    """Runtime option for agent sessions."""

    id: str
    display_name: str


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


class FileWriteRequest(BaseModel):
    """Request body for writing file content."""

    path: str
    content: str


# --- Schedule ---


class ScheduleEntry(BaseModel):
    """A single schedule item for today."""

    id: str  # "{entity}-{HH:MM}" or slugified personal ID
    entity: str
    time: str  # "HH:MM"
    label: str
    type: str = "heartbeat"  # "heartbeat" | "personal"
    done: bool = False


class PersonalScheduleCreate(BaseModel):
    """Request body for creating a personal schedule item."""

    time: str  # "HH:MM"
    label: str
    recurring: bool = False
    days: list[int] | None = None  # 0=Sun, 1=Mon, ..., 6=Sat


# --- Activity ---


class ActivityEvent(BaseModel):
    """Unified activity timeline event."""

    ts: float
    type: str  # "action" | "delivery" | "heartbeat"
    entity: str
    summary: str


# --- Notes ---


class NoteItem(BaseModel):
    """Note list entry (preview)."""

    id: str  # relative path from ~/notes/
    title: str
    preview: str = ""
    modified: str = ""  # ISO 8601


class NoteDetail(BaseModel):
    """Full note content."""

    id: str
    title: str
    content: str
    modified: str = ""


class NoteCreate(BaseModel):
    """Request body for creating a note."""

    title: str
    content: str
    subdir: str = ""


class NoteUpdate(BaseModel):
    """Request body for updating a note."""

    content: str


# --- Rooms ---


class RoomMessage(BaseModel):
    """A single message in a room transcript."""

    id: str
    sender: str
    recipients: list[str]
    mode: str  # "directed" | "broadcast" | "response"
    content: str
    timestamp: float


class Room(BaseModel):
    """Meeting room with participants and transcript."""

    id: str
    title: str
    description: str = ""
    moderator: str
    participants: list[str]
    state: str = "active"
    transcript: list[RoomMessage] = Field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0


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
