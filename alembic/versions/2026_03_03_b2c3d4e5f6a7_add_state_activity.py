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
    # Extend agent_states with new nullable columns
    op.add_column("agent_states", sa.Column("entity", sa.Text(), nullable=True))
    op.add_column("agent_states", sa.Column("context", sa.Text(), nullable=True))
    op.add_column("agent_states", sa.Column("ts", sa.Text(), nullable=True))
    op.add_column("agent_states", sa.Column("plan_file", sa.Text(), nullable=True))
    op.add_column("agent_states", sa.Column("plan_title", sa.Text(), nullable=True))

    # Create agent_activity table
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
    op.create_index("idx_activity_session_ts", "agent_activity", ["session", "ts"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_activity_session_ts", table_name="agent_activity")
    op.drop_table("agent_activity")
    op.drop_column("agent_states", "plan_title")
    op.drop_column("agent_states", "plan_file")
    op.drop_column("agent_states", "ts")
    op.drop_column("agent_states", "context")
    op.drop_column("agent_states", "entity")
