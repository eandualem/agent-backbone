"""Pydantic response models for the REST API.

Domain-organized submodules with barrel re-exports for backward compatibility.
All existing ``from agent_backbone.api.models import X`` imports continue to work.
"""

from agent_backbone.api.models._activity import ActivityEvent
from agent_backbone.api.models._agents import (
    ActivityCreateRequest,
    AgentActivityEvent,
    AgentStartRequest,
    AgentStartResponse,
    AgentStateDetail,
    AgentStopResponse,
    EnrichedAgent,
    StateUpdateRequest,
)
from agent_backbone.api.models._base import ErrorDetail, ListEnvelope
from agent_backbone.api.models._deliveries import DeliveryRecord, DeliveryStats
from agent_backbone.api.models._files import FileNode, FileWriteRequest
from agent_backbone.api.models._hierarchy import (
    CodingAgentNode,
    HierarchyNode,
    HierarchyResponse,
    HierarchySection,
    HierarchyState,
    HierarchySwarmWorkerNode,
    HierarchyTier,
)
from agent_backbone.api.models._issues import (
    IssueCommentResponse,
    IssueDependencies,
    IssueDetail,
    IssueResponse,
    IssueSummary,
    ParsedLabelsResponse,
)
from agent_backbone.api.models._messages import MessageRequest, MessageResponse
from agent_backbone.api.models._notes import NoteCreate, NoteDetail, NoteItem, NoteUpdate
from agent_backbone.api.models._repos import (
    CheckDetail,
    OnboardingStepDetail,
    RepoOnboardRequest,
    RepoOnboardResponse,
    RepoStatusResponse,
)
from agent_backbone.api.models._rooms import (
    BroadcastMessageRequest,
    DirectedMessageRequest,
    ResponseMessageRequest,
    Room,
    RoomCreate,
    RoomMessage,
    RoomStateUpdate,
)
from agent_backbone.api.models._status import (
    DashboardCounts,
    DashboardResponse,
    RuntimeInfo,
    ServiceHealth,
    SystemDigest,
)
from agent_backbone.api.models._swarms import (
    SwarmAssignmentCreateRequest,
    SwarmAssignmentDispatchResponse,
    SwarmAssignmentResponse,
    SwarmAssignmentStatus,
    SwarmBroadcastRequest,
    SwarmBroadcastResponse,
    SwarmCreateRequest,
    SwarmCreateResponse,
    SwarmDetailResponse,
    SwarmMessageLogEntry,
    SwarmMessageRequest,
    SwarmPhase,
    SwarmPhaseHistoryResponse,
    SwarmPhaseUpdateRequest,
    SwarmProgress,
    SwarmSummaryResponse,
    SwarmWorkerCompleteRequest,
    SwarmWorkerCreateRequest,
    SwarmWorkerResponse,
    SwarmWorkerRole,
    SwarmWorkerStatus,
    SwarmWorkerStatusUpdateRequest,
)
from agent_backbone.api.models._tracks import (
    RunActionRequest,
    RunActionResponse,
    RunCreate,
    RunResponse,
    RunUpdate,
    TrackCreate,
    TrackLayoutRequest,
    TrackLayoutResponse,
    TrackResponse,
    TrackUpdate,
)

__all__ = [
    # Base
    "ErrorDetail",
    "ListEnvelope",
    # Agents
    "ActivityCreateRequest",
    "AgentActivityEvent",
    "AgentStartRequest",
    "AgentStartResponse",
    "AgentStateDetail",
    "AgentStopResponse",
    "EnrichedAgent",
    "StateUpdateRequest",
    # Issues
    "IssueCommentResponse",
    "IssueDependencies",
    "IssueDetail",
    "IssueResponse",
    "IssueSummary",
    "ParsedLabelsResponse",
    # Messages
    "MessageRequest",
    "MessageResponse",
    # Deliveries
    "DeliveryRecord",
    "DeliveryStats",
    # Hierarchy
    "CodingAgentNode",
    "HierarchyNode",
    "HierarchyResponse",
    "HierarchySection",
    "HierarchyState",
    "HierarchySwarmWorkerNode",
    "HierarchyTier",
    # Swarms
    "SwarmAssignmentCreateRequest",
    "SwarmAssignmentDispatchResponse",
    "SwarmAssignmentResponse",
    "SwarmAssignmentStatus",
    "SwarmBroadcastRequest",
    "SwarmBroadcastResponse",
    "SwarmCreateRequest",
    "SwarmCreateResponse",
    "SwarmDetailResponse",
    "SwarmMessageLogEntry",
    "SwarmMessageRequest",
    "SwarmPhase",
    "SwarmPhaseHistoryResponse",
    "SwarmPhaseUpdateRequest",
    "SwarmProgress",
    "SwarmSummaryResponse",
    "SwarmWorkerCompleteRequest",
    "SwarmWorkerCreateRequest",
    "SwarmWorkerResponse",
    "SwarmWorkerRole",
    "SwarmWorkerStatus",
    "SwarmWorkerStatusUpdateRequest",
    # Status
    "DashboardCounts",
    "DashboardResponse",
    "RuntimeInfo",
    "ServiceHealth",
    "SystemDigest",
    # Files
    "FileNode",
    "FileWriteRequest",
    # Activity
    "ActivityEvent",
    # Notes
    "NoteCreate",
    "NoteDetail",
    "NoteItem",
    "NoteUpdate",
    # Rooms
    "BroadcastMessageRequest",
    "DirectedMessageRequest",
    "ResponseMessageRequest",
    "Room",
    "RoomCreate",
    "RoomMessage",
    "RoomStateUpdate",
    # Repos
    "CheckDetail",
    "OnboardingStepDetail",
    "RepoOnboardRequest",
    "RepoOnboardResponse",
    "RepoStatusResponse",
    # Tracks
    "RunActionRequest",
    "RunActionResponse",
    "RunCreate",
    "RunResponse",
    "RunUpdate",
    "TrackCreate",
    "TrackLayoutRequest",
    "TrackLayoutResponse",
    "TrackResponse",
    "TrackUpdate",
]
