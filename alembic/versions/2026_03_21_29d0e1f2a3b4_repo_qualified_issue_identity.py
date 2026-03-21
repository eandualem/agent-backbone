"""add repo-qualified issue identity

Revision ID: 29d0e1f2a3b4
Revises: 18c9d0e1f2a3
Create Date: 2026-03-21 07:45:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "29d0e1f2a3b4"
down_revision: str | None = "18c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_DEFAULT_REPO_FULL_NAME = "eandualem/orchestration"


def _backfill_repo_full_name(conn: sa.Connection, table_name: str) -> None:
    conn.execute(
        text(
            f"""
            UPDATE {table_name}
            SET repo_full_name = :repo_full_name
            WHERE repo_full_name IS NULL OR repo_full_name = ''
            """
        ),
        {"repo_full_name": _LEGACY_DEFAULT_REPO_FULL_NAME},
    )


def upgrade() -> None:
    op.add_column(
        "deliveries",
        sa.Column("repo_full_name", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "message_queue",
        sa.Column("repo_full_name", sa.Text(), nullable=False, server_default=sa.text("''")),
    )

    op.create_table(
        "acknowledgments_repo_tmp",
        sa.Column(
            "repo_full_name",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("target_entity", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("repo_full_name", "issue_number", "target_entity"),
    )

    conn = op.get_bind()
    _backfill_repo_full_name(conn, "deliveries")
    _backfill_repo_full_name(conn, "message_queue")
    conn.execute(
        text(
            """
            INSERT INTO acknowledgments_repo_tmp
                (repo_full_name, issue_number, target_entity, acknowledged_at)
            SELECT
                :repo_full_name,
                issue_number,
                target_entity,
                acknowledged_at
            FROM acknowledgments
            """
        ),
        {"repo_full_name": _LEGACY_DEFAULT_REPO_FULL_NAME},
    )

    op.drop_table("acknowledgments")
    op.rename_table("acknowledgments_repo_tmp", "acknowledgments")

    op.drop_index("uq_deliveries_active_owner", table_name="deliveries")
    op.drop_index("uq_mq_issue_dedup", table_name="message_queue")
    op.drop_index("uq_mq_comment_dedup", table_name="message_queue")

    op.create_index(
        "uq_deliveries_active_owner",
        "deliveries",
        ["repo_full_name", "issue_number", "session_name"],
        unique=True,
        postgresql_where=text("outcome IN ('attempting','delivered','retried')"),
        sqlite_where=text("outcome IN ('attempting','delivered','retried')"),
    )
    op.create_index(
        "uq_mq_issue_dedup",
        "message_queue",
        ["session_name", "repo_full_name", "issue_number"],
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
        ["session_name", "repo_full_name", "issue_number", "content_hash"],
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


def downgrade() -> None:
    op.drop_index("uq_mq_comment_dedup", table_name="message_queue")
    op.drop_index("uq_mq_issue_dedup", table_name="message_queue")
    op.drop_index("uq_deliveries_active_owner", table_name="deliveries")

    op.create_index(
        "uq_deliveries_active_owner",
        "deliveries",
        ["issue_number", "session_name"],
        unique=True,
        postgresql_where=text("outcome IN ('attempting','delivered','retried')"),
        sqlite_where=text("outcome IN ('attempting','delivered','retried')"),
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

    op.create_table(
        "acknowledgments_legacy_tmp",
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("target_entity", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("issue_number", "target_entity"),
    )

    conn = op.get_bind()
    conn.execute(
        text(
            """
            INSERT INTO acknowledgments_legacy_tmp (issue_number, target_entity, acknowledged_at)
            SELECT issue_number, target_entity, MAX(acknowledged_at)
            FROM acknowledgments
            GROUP BY issue_number, target_entity
            """
        )
    )

    op.drop_table("acknowledgments")
    op.rename_table("acknowledgments_legacy_tmp", "acknowledgments")

    with op.batch_alter_table("message_queue") as batch_op:
        batch_op.drop_column("repo_full_name")
    with op.batch_alter_table("deliveries") as batch_op:
        batch_op.drop_column("repo_full_name")
