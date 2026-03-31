"""Swarm-related API models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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
