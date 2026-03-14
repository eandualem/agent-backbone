"""Pydantic response models for the REST API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    figure: str = ""
    org: str = ""
    groups: list[str] = Field(default_factory=list)
    home: str = ""
    type: str = "coding_agent"  # "named_entity" | "coding_agent"
    entity_type: str = "agent"  # "agent" | "service"
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
    state: str = "unknown"
    current_issue: int | None = None
    timestamp: float = 0.0
    source: str = "default"
    started_at: float | None = None
    plan_file: str | None = None
    plan_title: str | None = None


class StateUpdateRequest(BaseModel):
    """Request body for updating agent state via API."""

    entity: str = ""
    state: str
    issue: int | None = None
    context: str = ""
    ts: float = 0.0
    plan_file: str | None = None
    plan_title: str | None = None


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


class PlanRejectRequest(BaseModel):
    """Request body for rejecting a plan with feedback."""

    feedback: str


class PlanRespondRequest(BaseModel):
    """Request body for responding to a plan prompt (option selection or free text)."""

    input: str


# --- Messages ---


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


# --- Hierarchy ---


HierarchyTier = Literal[
    "root",
    "assistant",
    "strategic",
    "orchestrator",
    "sub-orchestrator",
    "independent-peer",
    "knowledge-worker",
    "coding-agent",
    "swarm-worker",
]

HierarchyState = Literal["idle", "busy", "processing", "plan_waiting", "offline", "starting"]


class HierarchySwarmWorkerNode(BaseModel):
    """Swarm worker attached under a coding agent."""

    id: str
    name: str
    role: str
    session: str
    branch: str
    status: str


class CodingAgentNode(BaseModel):
    """Dynamic coding agent attached to a hierarchy node."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    label: str
    org: str
    state: HierarchyState
    online: bool
    current_issue: int | None = Field(
        default=None,
        alias="currentIssue",
        serialization_alias="currentIssue",
    )
    swarm_workers: list[HierarchySwarmWorkerNode] | None = Field(
        default=None,
        alias="swarmWorkers",
        serialization_alias="swarmWorkers",
    )


class HierarchyNode(BaseModel):
    """Static named node in the organizational hierarchy."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    label: str
    role: str
    tier: HierarchyTier
    state: HierarchyState
    session: str | None
    online: bool
    managed_org: str | None = Field(
        default=None,
        alias="managedOrg",
        serialization_alias="managedOrg",
    )
    children: list[HierarchyNode] | None = None
    coding_agents: list[CodingAgentNode] | None = Field(
        default=None,
        alias="codingAgents",
        serialization_alias="codingAgents",
    )


class HierarchySection(BaseModel):
    """Display section for rendering named hierarchy groups."""

    id: str
    title: str
    nodes: list[HierarchyNode] = Field(default_factory=list)


class HierarchyResponse(BaseModel):
    """Full hierarchy response for the org-chart API."""

    model_config = ConfigDict(populate_by_name=True)

    root: HierarchyNode
    strategy: HierarchyNode
    independent_peers: list[HierarchyNode] = Field(
        default_factory=list,
        alias="independentPeers",
        serialization_alias="independentPeers",
    )
    sections: list[HierarchySection] = Field(default_factory=list)
    unassigned_coding_agents: list[CodingAgentNode] = Field(
        default_factory=list,
        alias="unassignedCodingAgents",
        serialization_alias="unassignedCodingAgents",
    )


HierarchyNode.model_rebuild()


# --- Swarms ---


SwarmPhase = Literal[
    "created",
    "planning",
    "working",
    "validating",
    "pr_open",
    "awaiting_review",
    "merged",
    "cleaned_up",
    "failed",
    "discarded",
]
SwarmWorkerRole = Literal["lead", "coder", "tester", "validator", "scout"]
SwarmWorkerStatus = Literal["pending", "started", "working", "pr_created", "done", "failed"]
SwarmAssignmentStatus = Literal["active", "completed", "superseded", "cancelled"]


class SwarmWorkerCreateRequest(BaseModel):
    """Worker registration payload for swarm creation."""

    name: str
    role: SwarmWorkerRole
    branch: str
    worktree_path: str
    session: str


class SwarmCreateRequest(BaseModel):
    """Request body for creating a swarm."""

    repo: str
    task_id: str | None = None
    coding_agent_session: str
    workers: list[SwarmWorkerCreateRequest] = Field(default_factory=list)


class SwarmCreateResponse(BaseModel):
    """Response from swarm creation."""

    swarm_id: str


class SwarmProgress(BaseModel):
    """Aggregated worker progress for one swarm."""

    total: int = 0
    finished: int = 0
    percent: float = 0.0
    pending: int = 0
    started: int = 0
    working: int = 0
    pr_created: int = 0
    done: int = 0
    failed: int = 0


class SwarmWorkerResponse(BaseModel):
    """Worker record in swarm detail responses."""

    worker_id: str
    swarm_id: str
    name: str
    role: SwarmWorkerRole
    branch: str
    worktree_path: str
    session: str
    status: SwarmWorkerStatus
    pr_number: int | None = None
    summary: str | None = None
    failure_reason: str | None = None
    completed_at: str | None = None
    created_at: str
    updated_at: str


class SwarmAssignmentResponse(BaseModel):
    """One persisted worker assignment within a swarm."""

    assignment_id: int
    swarm_id: str
    worker_name: str
    assigned_by: str
    summary: str
    file_paths: list[str] = Field(default_factory=list)
    status: SwarmAssignmentStatus
    created_at: str
    completed_at: str | None = None


class SwarmPhaseHistoryResponse(BaseModel):
    """One swarm phase transition record."""

    history_id: int
    swarm_id: str
    from_phase: SwarmPhase | None = None
    to_phase: SwarmPhase
    timestamp: str
    triggered_by: str


class SwarmSummaryResponse(BaseModel):
    """Swarm summary for list responses."""

    swarm_id: str
    repo: str
    task_id: str | None = None
    coding_agent_session: str
    phase: SwarmPhase
    created_at: str
    completed_at: str | None = None
    worker_count: int = 0
    progress: SwarmProgress = Field(default_factory=SwarmProgress)


class SwarmDetailResponse(SwarmSummaryResponse):
    """Full swarm detail including workers."""

    workers: list[SwarmWorkerResponse] = Field(default_factory=list)
    workers_by_role: dict[SwarmWorkerRole, list[SwarmWorkerResponse]] = Field(default_factory=dict)
    assignments: list[SwarmAssignmentResponse] = Field(default_factory=list)
    phase_history: list[SwarmPhaseHistoryResponse] = Field(default_factory=list)


class SwarmWorkerStatusUpdateRequest(BaseModel):
    """Request body for worker status updates."""

    status: SwarmWorkerStatus
    pr_number: int | None = None


class SwarmPhaseUpdateRequest(BaseModel):
    """Request body for swarm phase updates."""

    phase: SwarmPhase


class SwarmWorkerCompleteRequest(BaseModel):
    """Request body for worker completion updates."""

    status: Literal["done", "failed"]
    summary: str = Field(min_length=1)
    pr_number: int | None = None


class SwarmAssignmentCreateRequest(BaseModel):
    """Request body for lead-issued worker assignments."""

    worker_name: str
    from_entity: str
    summary: str = Field(min_length=1)
    file_paths: list[str] = Field(default_factory=list)

    @field_validator("file_paths")
    @classmethod
    def validate_file_paths(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_path in value:
            path = raw_path.strip()
            if not path:
                raise ValueError("file_paths cannot include empty paths")
            if path not in seen:
                normalized.append(path)
                seen.add(path)
        return normalized


class SwarmMessageRequest(BaseModel):
    """Request body for directed swarm messages."""

    role: SwarmWorkerRole | None = None
    worker_name: str | None = None
    from_entity: str
    message: str

    @model_validator(mode="after")
    def validate_target(self) -> SwarmMessageRequest:
        if (self.role is None) == (self.worker_name is None):
            raise ValueError("Exactly one of role or worker_name is required")
        return self


class SwarmMessageLogEntry(BaseModel):
    """Recorded message delivered within a swarm."""

    message_id: int
    swarm_id: str
    target_kind: Literal["broadcast", "role", "worker"]
    target_role: SwarmWorkerRole | None = None
    target_worker_name: str | None = None
    from_entity: str
    message: str
    delivered: int
    failed: int
    total: int
    created_at: str


class SwarmBroadcastRequest(BaseModel):
    """Request body for swarm lead broadcasts."""

    from_entity: str
    message: str


class SwarmBroadcastResponse(BaseModel):
    """Response from swarm broadcast delivery."""

    ok: bool
    message_id: int | None = None
    delivered: int
    failed: int
    total: int


class SwarmAssignmentDispatchResponse(BaseModel):
    """Response from creating and dispatching one worker assignment."""

    ok: bool
    assignment_id: int
    message_id: int | None = None
    delivered: int
    failed: int
    total: int
    assignment: SwarmAssignmentResponse


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


class DashboardCounts(BaseModel):
    """Aggregate agent counts by state."""

    total: int = 0
    active: int = 0
    idle: int = 0
    busy: int = 0
    plan_waiting: int = 0
    offline: int = 0


class DashboardResponse(BaseModel):
    """Unified dashboard payload — everything the dashboard needs in one request."""

    agents: list[EnrichedAgent] = Field(default_factory=list)
    counts: DashboardCounts = Field(default_factory=DashboardCounts)
    plans_pending: int = 0
    issues_pending: int = 0
    failed_deliveries: int = 0
    services: ServiceHealth = Field(default_factory=ServiceHealth)


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


class RuntimeInfo(BaseModel):
    """Runtime option for agent sessions."""

    id: str
    display_name: str
    available: bool = True


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


class FileWriteRequest(BaseModel):
    """Request body for writing file content."""

    path: str
    content: str


class FileNode(BaseModel):
    """File or directory entry."""

    name: str
    type: str  # "file" or "directory"
    path: str
    children: list[FileNode] | None = None


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


class NoteCreate(BaseModel):
    """Request body for creating a note."""

    title: str
    content: str
    subdir: str = ""


class NoteUpdate(BaseModel):
    """Request body for updating a note."""

    content: str


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


# --- Rooms ---


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


# --- Repos / Onboarding ---


class CheckDetail(BaseModel):
    """Single onboarding status check result."""

    check: int  # 1-7
    name: str
    status: str  # "ok" | "missing" | "info"
    path: str = ""
    detail: str = ""


class RepoStatusResponse(BaseModel):
    """Repository onboarding status with individual checks."""

    org: str
    repo: str
    onboarded: bool = False
    checks: list[CheckDetail] = Field(default_factory=list)


class RepoOnboardRequest(BaseModel):
    """Request body for onboarding a new repository."""

    org: str
    url: str  # SSH URL, e.g. git@github.com:eandualem/repo.git


class OnboardingStepDetail(BaseModel):
    """Single onboarding step result."""

    step: int
    name: str
    status: str  # "done" | "skipped" | "failed" | "manual_required"
    detail: str = ""
    command: str | None = None


class RepoOnboardResponse(BaseModel):
    """Onboarding execution result."""

    org: str
    repo: str
    success: bool = False
    error: str = ""
    steps: list[OnboardingStepDetail] = Field(default_factory=list)


# --- Analytics ---


class AnalyticsScope(BaseModel):
    """Scope metadata for an analytics query."""

    session: str
    since: float | None = None
    until: float | None = None
    include_swarm_workers: bool = True
    sessions_queried: list[str] = Field(default_factory=list)


class AnalyticsDuration(BaseModel):
    """Duration summary (all values are best-effort)."""

    observed_window_seconds: float = 0.0
    session_seconds: float = 0.0
    task_seconds: float = 0.0
    tool_seconds: float = 0.0
    best_effort: bool = True


class AnalyticsTotals(BaseModel):
    """Aggregate totals for an analytics overview."""

    tokens: dict[str, int] = Field(default_factory=dict)
    captured_cost_usd: float = 0.0
    cost_status: str = "unavailable"
    errors: dict[str, int] = Field(default_factory=dict)
    tool_count: int = 0
    duration: AnalyticsDuration = Field(
        default_factory=AnalyticsDuration,
    )


class AnalyticsSessionBreakdown(BaseModel):
    """Per-session breakdown within an overview."""

    model_config = ConfigDict(extra="allow")

    session: str
    event_count: int = 0
    tokens: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0
    error_count: int = 0
    tool_count: int = 0
    is_swarm_worker: bool = False
    swarm_id: str | None = None
    swarm_worker_name: str | None = None
    swarm_worker_role: str | None = None
    coding_agent_session: str | None = None
    repo: str | None = None
    task_id: str | None = None


class AnalyticsOverviewResponse(BaseModel):
    """Response for GET /api/analytics/agents/{session}/overview."""

    scope: AnalyticsScope
    rows_scanned: int = 0
    deduped_rows: int = 0
    totals: AnalyticsTotals = Field(
        default_factory=AnalyticsTotals,
    )
    breakdown_by_session: list[AnalyticsSessionBreakdown] = Field(
        default_factory=list,
    )


class AnalyticsEventEntry(BaseModel):
    """Single event in the analytics event feed."""

    model_config = ConfigDict(extra="allow")

    id: int
    session: str
    event: str
    entity: str | None = None
    runtime: str | None = None
    model: str | None = None
    source_kind: str | None = None
    source_ref: str | None = None
    source_event_id: str | None = None
    trace_id: str | None = None
    parent_trace_id: str | None = None
    ts: str
    data: dict = Field(default_factory=dict)
    is_swarm_worker: bool = False
    swarm_id: str | None = None
    swarm_worker_name: str | None = None
    swarm_worker_role: str | None = None
    coding_agent_session: str | None = None
    repo: str | None = None
    task_id: str | None = None


class AnalyticsCursor(BaseModel):
    """Pagination cursor for the event feed."""

    ts: str
    id: int


class AnalyticsEventsResponse(BaseModel):
    """Response for GET /api/analytics/agents/{session}/events."""

    events: list[AnalyticsEventEntry] = Field(
        default_factory=list,
    )
    has_more: bool = False
    next_cursor: AnalyticsCursor | None = None


class AnalyticsToolStat(BaseModel):
    """Aggregated stats for one tool."""

    tool_name: str
    call_count: int = 0
    error_count: int = 0
    total_duration_seconds: float = 0.0


class AnalyticsToolsResponse(BaseModel):
    """Response for GET /api/analytics/agents/{session}/tools."""

    tools: list[AnalyticsToolStat] = Field(default_factory=list)
    total_tools: int = 0


class AnalyticsErrorEntry(BaseModel):
    """Single error in the analytics error feed."""

    model_config = ConfigDict(extra="allow")

    session: str
    event: str
    ts: str
    error_type: str = "unknown"
    message: str = ""
    tool_name: str | None = None
    is_swarm_worker: bool = False
    swarm_id: str | None = None
    swarm_worker_name: str | None = None
    swarm_worker_role: str | None = None
    coding_agent_session: str | None = None
    repo: str | None = None
    task_id: str | None = None


class AnalyticsErrorsResponse(BaseModel):
    """Response for GET /api/analytics/agents/{session}/errors."""

    summary: dict[str, int] = Field(default_factory=dict)
    total_errors: int = 0
    errors: list[AnalyticsErrorEntry] = Field(
        default_factory=list,
    )
