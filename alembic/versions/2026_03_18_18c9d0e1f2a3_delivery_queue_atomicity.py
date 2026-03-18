"""add delivery queue atomicity

Revision ID: 18c9d0e1f2a3
Revises: 07b8c9d0e1f2
Create Date: 2026-03-18 09:00:00.000000
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "18c9d0e1f2a3"
down_revision: str | None = "07b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sha256(message: str | None) -> str:
    return hashlib.sha256((message or "").encode("utf-8")).hexdigest()


def _backfill_content_hash(conn: sa.Connection) -> None:
    rows = conn.execute(
        text("SELECT id, message FROM message_queue WHERE content_hash IS NULL")
    ).mappings()
    for row in rows:
        conn.execute(
            text("UPDATE message_queue SET content_hash = :content_hash WHERE id = :row_id"),
            {"content_hash": _sha256(row["message"]), "row_id": row["id"]},
        )


def _cleanup_duplicate_deliveries(conn: sa.Connection) -> None:
    conn.execute(
        text(
            """
            DELETE FROM deliveries
            WHERE outcome IN ('attempting','delivered','retried')
              AND EXISTS (
                  SELECT 1
                  FROM deliveries AS newer
                  WHERE newer.issue_number = deliveries.issue_number
                    AND newer.session_name = deliveries.session_name
                    AND newer.outcome IN ('attempting','delivered','retried')
                    AND newer.id > deliveries.id
              )
            """
        )
    )


def _cleanup_duplicate_issue_queue_entries(conn: sa.Connection) -> None:
    conn.execute(
        text(
            """
            DELETE FROM message_queue
            WHERE delivery_kind = 'issue'
              AND status IN ('pending','in_progress')
              AND issue_number IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM message_queue AS newer
                  WHERE newer.delivery_kind = 'issue'
                    AND newer.status IN ('pending','in_progress')
                    AND newer.issue_number IS NOT NULL
                    AND newer.session_name = message_queue.session_name
                    AND newer.issue_number = message_queue.issue_number
                    AND newer.id > message_queue.id
              )
            """
        )
    )


def _cleanup_duplicate_comment_queue_entries(conn: sa.Connection) -> None:
    conn.execute(
        text(
            """
            DELETE FROM message_queue
            WHERE delivery_kind = 'comment'
              AND status IN ('pending','in_progress')
              AND issue_number IS NOT NULL
              AND content_hash IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM message_queue AS newer
                  WHERE newer.delivery_kind = 'comment'
                    AND newer.status IN ('pending','in_progress')
                    AND newer.issue_number IS NOT NULL
                    AND newer.content_hash IS NOT NULL
                    AND newer.session_name = message_queue.session_name
                    AND newer.issue_number = message_queue.issue_number
                    AND newer.content_hash = message_queue.content_hash
                    AND newer.id > message_queue.id
              )
            """
        )
    )


def _cleanup_duplicate_direct_messages(conn: sa.Connection) -> None:
    conn.execute(
        text(
            """
            DELETE FROM message_queue
            WHERE delivery_kind = 'direct_message'
              AND status IN ('pending','in_progress')
              AND content_hash IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM message_queue AS newer
                  WHERE newer.delivery_kind = 'direct_message'
                    AND newer.status IN ('pending','in_progress')
                    AND newer.content_hash IS NOT NULL
                    AND newer.session_name = message_queue.session_name
                    AND newer.content_hash = message_queue.content_hash
                    AND newer.id > message_queue.id
              )
            """
        )
    )


def upgrade() -> None:
    op.add_column("message_queue", sa.Column("content_hash", sa.Text(), nullable=True))
    op.add_column("message_queue", sa.Column("leased_at", sa.Text(), nullable=True))

    conn = op.get_bind()
    _backfill_content_hash(conn)
    _cleanup_duplicate_deliveries(conn)
    _cleanup_duplicate_issue_queue_entries(conn)
    _cleanup_duplicate_comment_queue_entries(conn)
    _cleanup_duplicate_direct_messages(conn)

    op.create_index(
        "uq_deliveries_active_owner",
        "deliveries",
        ["issue_number", "session_name"],
        unique=True,
        postgresql_where=text("outcome IN ('attempting','delivered','retried')"),
        sqlite_where=text("outcome IN ('attempting','delivered','retried')"),
    )
    op.create_index(
        "idx_mq_leased",
        "message_queue",
        ["leased_at"],
        unique=False,
        postgresql_where=text("status = 'in_progress'"),
        sqlite_where=text("status = 'in_progress'"),
    )
    op.create_index(
        "uq_mq_issue_dedup",
        "message_queue",
        ["session_name", "issue_number"],
        unique=True,
        postgresql_where=text(
            "delivery_kind = 'issue' AND status IN ('pending','in_progress') "
            "AND issue_number IS NOT NULL"
        ),
        sqlite_where=text(
            "delivery_kind = 'issue' AND status IN ('pending','in_progress') "
            "AND issue_number IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_mq_comment_dedup",
        "message_queue",
        ["session_name", "issue_number", "content_hash"],
        unique=True,
        postgresql_where=text(
            "delivery_kind = 'comment' AND status IN ('pending','in_progress') "
            "AND issue_number IS NOT NULL"
        ),
        sqlite_where=text(
            "delivery_kind = 'comment' AND status IN ('pending','in_progress') "
            "AND issue_number IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_mq_dm_dedup",
        "message_queue",
        ["session_name", "content_hash"],
        unique=True,
        postgresql_where=text(
            "delivery_kind = 'direct_message' AND status IN ('pending','in_progress')"
        ),
        sqlite_where=text(
            "delivery_kind = 'direct_message' AND status IN ('pending','in_progress')"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_mq_dm_dedup", table_name="message_queue")
    op.drop_index("uq_mq_comment_dedup", table_name="message_queue")
    op.drop_index("uq_mq_issue_dedup", table_name="message_queue")
    op.drop_index("idx_mq_leased", table_name="message_queue")
    op.drop_index("uq_deliveries_active_owner", table_name="deliveries")

    with op.batch_alter_table("message_queue") as batch_op:
        batch_op.drop_column("leased_at")
        batch_op.drop_column("content_hash")
