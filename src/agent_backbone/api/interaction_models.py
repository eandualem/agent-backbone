"""Version-one read contract for captured interactions (docs/interactions.md)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Actor(Wire):
    kind: Literal["agent", "owner", "external", "unknown"]
    id: str | None
    name: str
    name_at_capture: str
    attribution: Literal["credential_bound", "operator_asserted", "signed", "unverified", "system"]
    retired: bool = False


class Body(Wire):
    level: Literal["full", "excerpt", "unavailable"]
    text: str | None
    reason: str | None
    format: Literal["text", "json"] = "text"


class Status(Wire):
    value: str
    observed_at: str | None
    evidence: str
    delivered: bool


class RelationTarget(Wire):
    kind: Literal["interaction", "report", "conversation", "owner_request"]
    id: str
    artifact_kind: str | None = None
    entry_id: int | None = None


class Relation(Wire):
    source_interaction_id: str | None = None
    type: Literal[
        "reply_to", "corrects", "for_owner_request", "asked_in_conversation", "from_report"
    ]
    target: RelationTarget
    recorded_at: str
    provenance: str


class LifecycleEvent(Wire):
    id: str
    kind: str
    time: str
    time_basis: Literal["recorded", "observed", "legacy"]
    outcome: str | None
    attempt_id: str | None
    source: str


class Attempt(Wire):
    id: str
    operation_id: str
    delivery_id: int | None
    queue_id: int | None
    outcome: str
    source: str
    recorded_at: str
    settled_at: str | None


class AttemptPage(Wire):
    items: list[Attempt]
    next_cursor: str | None


class EventPage(Wire):
    items: list[LifecycleEvent]
    next_cursor: str | None


class ConversationCapability(Wire):
    supported: bool
    kind: Literal["interaction"] = "interaction"
    artifact_id: str
    recipients: list[Actor]
    reason: str | None
    total: int
    last_entry_at: str | None


class ConversationEntryMetadata(Wire):
    kind: str
    artifact_id: str
    entry_id: int
    entry_kind: str
    created_at: str
    author_type: str
    author_id: str
    author_name: str
    recipient_id: str | None
    recipient_name: str | None
    in_reply_to: int | None
    anchor: dict | None
    anchored_interaction_id: str | None


class MessageCoverageInterval(Wire):
    since: str
    until: str
    completeness: Literal["complete_within_declared_scope"]


class Interaction(Wire):
    id: str
    operation_id: str
    accepted_at: str
    last_event_at: str
    kind: str
    sender: Actor
    recipient: Actor
    original_target: str
    preview: str
    body: Body
    status: Status
    attempt_count: int
    attempts: AttemptPage
    lifecycle: EventPage
    relations: list[Relation]
    relations_truncated: bool = False
    conversation: ConversationCapability
    conversation_entry: ConversationEntryMetadata | None = None


class Coverage(Wire):
    since: str
    until: str
    capture_started_at: str | None
    retained_from: str | None
    body_retained_from: str | None = None
    as_of: str
    completeness: Literal["partial", "complete_within_declared_scope", "unavailable"]
    reasons: list[str]
    declared_scope: list[str]
    truncated: bool
    state_history: Literal["observed_partial"] = "observed_partial"
    message_intervals: list[MessageCoverageInterval] = Field(default_factory=list)
    quiet_scope: Literal["capture_enabled_backbone_paths"] = "capture_enabled_backbone_paths"


class InteractionPage(Wire):
    schema_version: Literal[1] = 1
    items: list[Interaction]
    next_cursor: str | None
    snapshot: str
    coverage: Coverage


class Counts(Wire):
    sent: int
    received: int
    not_delivered: int


class Tick(Wire):
    interaction_id: str
    direction: Literal["sent", "received"]
    time: str
    time_basis: Literal["recorded", "observed", "legacy"]
    delivered: bool


class LastInteraction(Wire):
    id: str
    time: str


class AgentSummary(Wire):
    agent: Actor
    runtime: str
    state: str
    reason: str | None
    counts: Counts
    counterparties: list[Actor]
    latest: LastInteraction | None
    previous: LastInteraction | None
    ticks: list[Tick]
    ticks_truncated: bool
    conversation_supported: bool


class Summary(Wire):
    schema_version: Literal[1] = 1
    items: list[AgentSummary]
    snapshot: str
    coverage: Coverage
    conversation_supported: bool


class RelationInput(Wire):
    type: Literal[
        "reply_to", "corrects", "for_owner_request", "asked_in_conversation", "from_report"
    ]
    target: RelationTarget


class InteractionInput(Wire):
    """Optional attribution and explicit links on an existing send request."""

    actor: Literal["agent", "owner"] = "agent"
    relations: list[RelationInput] = Field(default_factory=list, max_length=20)


class ObservedStateSpan(Wire):
    id: str
    agent_id: str
    name_at_observation: str
    since: str
    until: str
    state: str
    reason: str | None
    first_observed_at: str
    last_observed_at: str
    observation_ids: list[str]
    basis: Literal["matching_samples", "point_observation"]
    time_basis: Literal["observed"] = "observed"
    evidence: list[str]


class StateCoverage(Wire):
    since: str
    until: str
    retained_from: str | None
    completeness: Literal["sampled_partial", "unavailable"]
    reasons: list[str]
    max_sample_gap_seconds: int
    legacy_name_only_excluded: bool
    unknown_outside_returned_spans: bool


class StateSpanPage(Wire):
    schema_version: Literal[1] = 1
    items: list[ObservedStateSpan]
    coverage: StateCoverage
    snapshot: str
    next_cursor: str | None
