"""ORM models for all backbone database tables."""

from __future__ import annotations

from sqlalchemy import Index, Integer, Text, text
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
        Index(
            "uq_deliveries_active_owner",
            "issue_number",
            "session_name",
            unique=True,
            postgresql_where=text("outcome IN ('attempting','delivered','retried')"),
            sqlite_where=text("outcome IN ('attempting','delivered','retried')"),
        ),
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
    leased_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

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
        Index(
            "uq_mq_comment_dedup",
            "session_name",
            "issue_number",
            "content_hash",
            unique=True,
            postgresql_where=text(
                "delivery_kind = 'comment' AND status IN ('pending','in_progress') "
                "AND issue_number IS NOT NULL"
            ),
            sqlite_where=text(
                "delivery_kind = 'comment' AND status IN ('pending','in_progress') "
                "AND issue_number IS NOT NULL"
            ),
        ),
        Index(
            "uq_mq_dm_dedup",
            "session_name",
            "content_hash",
            unique=True,
            postgresql_where=text(
                "delivery_kind = 'direct_message' AND status IN ('pending','in_progress')"
            ),
            sqlite_where=text(
                "delivery_kind = 'direct_message' AND status IN ('pending','in_progress')"
            ),
        ),
    )
