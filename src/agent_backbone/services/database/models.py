"""ORM models for all backbone database tables.

The database is the single source of truth for configuration (``settings``),
the known agents (``agents``, ``agent_watches``), inbound events, deliveries,
acknowledgments, the message queue and agent state. Issue-keyed tables carry
the repository (``owner/name``) so several repositories can share issue
numbers.
"""

from __future__ import annotations

from sqlalchemy import Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from agent_backbone.services.database.base import Base


class SettingORM(Base):
    """Configuration values (JSON-encoded) keyed by dotted name."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class AgentORM(Base):
    """A known agent: discovered on first start or registered through the CLI/API."""

    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    dir: Mapped[str] = mapped_column(Text, nullable=False)
    runtime: Mapped[str] = mapped_column(Text, nullable=False, server_default="claude")
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    tags: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    env: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    always_on: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    """1 when a dead session must be reported at once (``AgentSpec.always_on``)."""
    unattended: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    """1 when the runtime is launched with its no-approval switch (``AgentSpec.unattended``)."""
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_started_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentWatchORM(Base):
    """Repositories an agent watches (informational notifications + for: routing)."""

    __tablename__ = "agent_watches"

    agent_name: Mapped[str] = mapped_column(Text, primary_key=True)
    repo: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class SwarmORM(Base):
    """A swarm: one coordinator plus members sharing a worktree, working one issue.

    Members are ordinary agents (tagged ``swarm:<name>`` / ``role:<role>``);
    this table records only the swarm's lifecycle.
    """

    __tablename__ = "swarms"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    initiator: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    coordinator: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    worktree_dir: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # One active swarm per issue — enforced by the database so concurrent
        # creates cannot attach two swarms to the same issue.
        Index(
            "uq_swarms_active_issue",
            "repo",
            "issue_number",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )


class PollCursorORM(Base):
    """Replay boundary per repository, independent of event receipt times."""

    __tablename__ = "poll_cursors"

    repo: Mapped[str] = mapped_column(Text, primary_key=True)
    since: Mapped[str] = mapped_column(Text, nullable=False)


class EventORM(Base):
    """Every inbound event (webhook, poll, telegram, api) before/after routing."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sender: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    received_at: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    __table_args__ = (
        Index("uq_events_delivery_id", "delivery_id", unique=True),
        Index("idx_events_received", "received_at"),
        Index("idx_events_repo", "repo"),
    )


class DeliveryORM(Base):
    """Delivery tracking records (issue, comment, PR and direct messages)."""

    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="issue")
    repo: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_entity: Mapped[str] = mapped_column(Text, nullable=False)
    session_name: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    """Which code path made the attempt (``issue-dispatcher``, ``api-messages``, …)."""
    preview: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_deliveries_issue", "repo", "issue_number"),
        Index("idx_deliveries_entity", "target_entity"),
        Index("idx_deliveries_outcome", "outcome"),
        Index("idx_deliveries_created", "created_at"),
        Index(
            "uq_deliveries_active_owner",
            "repo",
            "issue_number",
            "session_name",
            unique=True,
            postgresql_where=text(
                "kind = 'issue' AND issue_number IS NOT NULL"
                " AND outcome IN ('attempting','delivered','retried')"
            ),
            sqlite_where=text(
                "kind = 'issue' AND issue_number IS NOT NULL"
                " AND outcome IN ('attempting','delivered','retried')"
            ),
        ),
    )


class AgentStateORM(Base):
    """Last known state per agent session (mirrors the hook state files)."""

    __tablename__ = "agent_states"

    session_name: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_issue: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_repo: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_title: Mapped[str | None] = mapped_column(Text, nullable=True)


class IssueDependencyORM(Base):
    """Parent/sub-issue dependency tracking (per repository)."""

    __tablename__ = "issue_dependencies"

    repo: Mapped[str] = mapped_column(Text, primary_key=True, server_default="")
    parent_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub_issue_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_deps_sub", "repo", "sub_issue_number"),)


class AcknowledgmentORM(Base):
    """Issue acknowledgment tracking (per repository)."""

    __tablename__ = "acknowledgments"

    repo: Mapped[str] = mapped_column(Text, primary_key=True, server_default="")
    issue_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_entity: Mapped[str] = mapped_column(Text, primary_key=True)
    acknowledged_at: Mapped[str] = mapped_column(Text, nullable=False)


class MessageQueueORM(Base):
    """Queued messages for deferred delivery."""

    __tablename__ = "message_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_name: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_entity: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="issue")
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    enqueued_at: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    leased_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    sender: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    """Who sent it (``from_entity``): part of the duplicate key, shown in the queue."""
    dedup_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    """What makes a non-issue message *the same* message: the source event's
    identity when the caller has one, else a hash of sender and text."""

    __table_args__ = (
        Index("idx_mq_status", "status"),
        Index("idx_mq_session", "session_name"),
        Index(
            "idx_mq_leased",
            "leased_at",
            postgresql_where=text("status = 'in_progress'"),
            sqlite_where=text("status = 'in_progress'"),
        ),
        Index(
            "uq_mq_issue_dedup",
            "session_name",
            "repo",
            "issue_number",
            unique=True,
            postgresql_where=text(
                "delivery_kind = 'issue' AND status IN ('pending','in_progress') "
                "AND issue_number IS NOT NULL"
            ),
            sqlite_where=text(
                "delivery_kind = 'issue' AND status IN ('pending','in_progress') "
                "AND issue_number IS NOT NULL"
            ),
        ),
        # One pending copy of the *same* message per session — the same
        # source event, or the same sender saying the same thing — so a
        # blocked drain re-offering it, or an accidental double send, never
        # grows the queue, while two senders with identical text both get in.
        Index(
            "uq_mq_message_dedup",
            "session_name",
            "dedup_key",
            unique=True,
            postgresql_where=text(
                "delivery_kind != 'issue' AND status IN ('pending','in_progress')"
            ),
            sqlite_where=text("delivery_kind != 'issue' AND status IN ('pending','in_progress')"),
        ),
    )
