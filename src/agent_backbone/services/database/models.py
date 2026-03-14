"""ORM models for all backbone database tables."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agent_backbone.services.database.base import Base


class DeliveryORM(Base):
    """Delivery tracking records."""

    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    target_entity: Mapped[str] = mapped_column(Text, nullable=False)
    session_name: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    flow_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    flow_run_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_deliveries_issue", "issue_number"),
        Index("idx_deliveries_entity", "target_entity"),
        Index("idx_deliveries_outcome", "outcome"),
        Index("idx_deliveries_created", "created_at"),
    )


class DedupLogORM(Base):
    """Webhook delivery deduplication log."""

    __tablename__ = "dedup_log"

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    received_at: Mapped[str] = mapped_column(Text, nullable=False)


class AgentStateORM(Base):
    """Agent state snapshots."""

    __tablename__ = "agent_states"

    session_name: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    current_issue: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_activity: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_title: Mapped[str | None] = mapped_column(Text, nullable=True)


class IssueDependencyORM(Base):
    """Parent/sub-issue dependency tracking."""

    __tablename__ = "issue_dependencies"

    parent_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub_issue_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_deps_sub", "sub_issue_number"),)


class AcknowledgmentORM(Base):
    """Issue acknowledgment tracking."""

    __tablename__ = "acknowledgments"

    issue_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_entity: Mapped[str] = mapped_column(Text, primary_key=True)
    acknowledged_at: Mapped[str] = mapped_column(Text, nullable=False)


class HeartbeatORM(Base):
    """Heartbeat delivery records."""

    __tablename__ = "heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_at: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("idx_heartbeats_agent", "agent"),)


class MessageQueueORM(Base):
    """Queued messages for deferred delivery."""

    __tablename__ = "message_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_name: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_entity: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="issue")
    flow_name: Mapped[str | None] = mapped_column(Text, server_default="")
    enqueued_at: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")

    __table_args__ = (
        Index("idx_mq_status", "status"),
        Index("idx_mq_session", "session_name"),
    )


class AgentActivityORM(Base):
    """Agent activity event log."""

    __tablename__ = "agent_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str | None] = mapped_column(Text, nullable=True)
    runtime: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_activity_session_ts", "session", "ts"),
        Index("idx_activity_runtime_ts", "runtime", "ts"),
        Index("idx_activity_trace", "trace_id"),
        Index("idx_activity_source", "source_ref", "source_event_id"),
    )


class TelemetryCheckpointORM(Base):
    """Incremental ingestion checkpoints for runtime telemetry sources."""

    __tablename__ = "telemetry_checkpoints"

    session: Mapped[str] = mapped_column(Text, primary_key=True)
    source_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    runtime: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    last_event_ts: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_telemetry_checkpoints_runtime", "runtime"),
        Index("idx_telemetry_checkpoints_updated", "updated_at"),
    )


class SwarmORM(Base):
    """Swarm lifecycle records."""

    __tablename__ = "swarms"

    swarm_id: Mapped[str] = mapped_column(Text, primary_key=True)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    coding_agent_session: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False, server_default="created")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "phase IN ('created', 'planning', 'working', 'validating', 'pr_open', "
            "'awaiting_review', 'merged', 'cleaned_up', 'failed', 'discarded')",
            name="ck_swarms_phase",
        ),
        Index("idx_swarms_repo", "repo"),
        Index("idx_swarms_phase", "phase"),
        Index("idx_swarms_created", "created_at"),
    )


class SwarmWorkerORM(Base):
    """Worker records for a swarm."""

    __tablename__ = "swarm_workers"

    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    swarm_id: Mapped[str] = mapped_column(ForeignKey("swarms.swarm_id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    worktree_path: Mapped[str] = mapped_column(Text, nullable=False)
    session: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("swarm_id", "name", name="uq_swarm_workers_swarm_name"),
        CheckConstraint(
            "role IN ('lead', 'coder', 'tester', 'validator', 'scout')",
            name="ck_swarm_workers_role",
        ),
        CheckConstraint(
            "status IN ('pending', 'started', 'working', 'pr_created', 'done', 'failed')",
            name="ck_swarm_workers_status",
        ),
        Index("idx_swarm_workers_swarm", "swarm_id"),
        Index("idx_swarm_workers_role", "role"),
        Index("idx_swarm_workers_session", "session"),
        Index("idx_swarm_workers_status", "status"),
    )


class SwarmPhaseHistoryORM(Base):
    """Phase transition history for swarms."""

    __tablename__ = "swarm_phase_history"

    history_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    swarm_id: Mapped[str] = mapped_column(ForeignKey("swarms.swarm_id"), nullable=False)
    from_phase: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_phase: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_swarm_phase_history_swarm", "swarm_id"),
        Index("idx_swarm_phase_history_timestamp", "timestamp"),
    )


class SwarmMessageORM(Base):
    """Logged messages exchanged within a swarm."""

    __tablename__ = "swarm_messages"

    message_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    swarm_id: Mapped[str] = mapped_column(ForeignKey("swarms.swarm_id"), nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_worker_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_entity: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    delivered: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('broadcast', 'role', 'worker')",
            name="ck_swarm_messages_target_kind",
        ),
        Index("idx_swarm_messages_swarm", "swarm_id"),
        Index("idx_swarm_messages_created", "created_at"),
    )


class SwarmAssignmentORM(Base):
    """Lead-issued worker assignments for collaborative swarms."""

    __tablename__ = "swarm_assignments"

    assignment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    swarm_id: Mapped[str] = mapped_column(ForeignKey("swarms.swarm_id"), nullable=False)
    worker_name: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_by: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    file_paths: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'completed', 'superseded', 'cancelled')",
            name="ck_swarm_assignments_status",
        ),
        Index("idx_swarm_assignments_swarm", "swarm_id"),
        Index("idx_swarm_assignments_worker", "worker_name"),
        Index("idx_swarm_assignments_status", "status"),
    )
