"""initial schema

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-02-28 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "acknowledgments",
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("target_entity", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("issue_number", "target_entity"),
    )
    op.create_table(
        "agent_states",
        sa.Column("session_name", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'unknown'"), nullable=False),
        sa.Column("current_issue", sa.Integer(), nullable=True),
        sa.Column("last_activity", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("session_name"),
    )
    op.create_table(
        "dedup_log",
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("target_entity", sa.Text(), nullable=False),
        sa.Column("session_name", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("flow_name", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("flow_run_id", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_deliveries_created", "deliveries", ["created_at"], unique=False)
    op.create_index("idx_deliveries_entity", "deliveries", ["target_entity"], unique=False)
    op.create_index("idx_deliveries_issue", "deliveries", ["issue_number"], unique=False)
    op.create_index("idx_deliveries_outcome", "deliveries", ["outcome"], unique=False)

    op.create_table(
        "heartbeats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("delivered_at", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_heartbeats_agent", "heartbeats", ["agent"], unique=False)

    op.create_table(
        "issue_dependencies",
        sa.Column("parent_number", sa.Integer(), nullable=False),
        sa.Column("sub_issue_number", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("parent_number", "sub_issue_number"),
    )
    op.create_index("idx_deps_sub", "issue_dependencies", ["sub_issue_number"], unique=False)

    op.create_table(
        "message_queue",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_name", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("target_entity", sa.Text(), nullable=True),
        sa.Column("flow_name", sa.Text(), server_default=sa.text("''"), nullable=True),
        sa.Column("enqueued_at", sa.Text(), nullable=False),
        sa.Column("delivered_at", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_mq_session", "message_queue", ["session_name"], unique=False)
    op.create_index("idx_mq_status", "message_queue", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_mq_status", table_name="message_queue")
    op.drop_index("idx_mq_session", table_name="message_queue")
    op.drop_table("message_queue")
    op.drop_index("idx_deps_sub", table_name="issue_dependencies")
    op.drop_table("issue_dependencies")
    op.drop_index("idx_heartbeats_agent", table_name="heartbeats")
    op.drop_table("heartbeats")
    op.drop_index("idx_deliveries_outcome", table_name="deliveries")
    op.drop_index("idx_deliveries_issue", table_name="deliveries")
    op.drop_index("idx_deliveries_entity", table_name="deliveries")
    op.drop_index("idx_deliveries_created", table_name="deliveries")
    op.drop_table("deliveries")
    op.drop_table("dedup_log")
    op.drop_table("agent_states")
    op.drop_table("acknowledgments")
