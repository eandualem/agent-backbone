"""add state columns and activity table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-03 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Extend agent_states with new nullable columns (idempotent)
    existing_columns = {c["name"] for c in inspector.get_columns("agent_states")}
    for col_name in ("entity", "context", "ts", "plan_file", "plan_title"):
        if col_name not in existing_columns:
            op.add_column("agent_states", sa.Column(col_name, sa.Text(), nullable=True))

    # Create agent_activity table (idempotent — may already exist from create_all)
    if "agent_activity" not in inspector.get_table_names():
        op.create_table(
            "agent_activity",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("session", sa.Text(), nullable=False),
            sa.Column("event", sa.Text(), nullable=False),
            sa.Column("data", sa.Text(), nullable=True),
            sa.Column("ts", sa.Text(), nullable=False),
            sa.Column("received_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("agent_activity")}
    if "idx_activity_session_ts" not in existing_indexes:
        op.create_index("idx_activity_session_ts", "agent_activity", ["session", "ts"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_activity_session_ts", table_name="agent_activity")
    op.drop_table("agent_activity")
    op.drop_column("agent_states", "plan_title")
    op.drop_column("agent_states", "plan_file")
    op.drop_column("agent_states", "ts")
    op.drop_column("agent_states", "context")
    op.drop_column("agent_states", "entity")
