"""add telemetry checkpoints and rich activity columns

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-13 15:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    activity_columns = {c["name"] for c in inspector.get_columns("agent_activity")}
    for col_name in (
        "entity",
        "runtime",
        "source_kind",
        "source_ref",
        "source_event_id",
        "trace_id",
        "parent_trace_id",
        "model",
    ):
        if col_name not in activity_columns:
            op.add_column("agent_activity", sa.Column(col_name, sa.Text(), nullable=True))

    if "telemetry_checkpoints" not in inspector.get_table_names():
        op.create_table(
            "telemetry_checkpoints",
            sa.Column("session", sa.Text(), nullable=False),
            sa.Column("source_ref", sa.Text(), nullable=False),
            sa.Column("runtime", sa.Text(), nullable=False),
            sa.Column("source_kind", sa.Text(), nullable=False),
            sa.Column("entity", sa.Text(), nullable=True),
            sa.Column("checkpoint", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("last_event_ts", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("session", "source_ref"),
        )

    inspector = sa.inspect(conn)
    activity_indexes = {idx["name"] for idx in inspector.get_indexes("agent_activity")}
    if "idx_activity_runtime_ts" not in activity_indexes:
        op.create_index(
            "idx_activity_runtime_ts",
            "agent_activity",
            ["runtime", "ts"],
            unique=False,
        )
    if "idx_activity_trace" not in activity_indexes:
        op.create_index("idx_activity_trace", "agent_activity", ["trace_id"], unique=False)
    if "idx_activity_source" not in activity_indexes:
        op.create_index(
            "idx_activity_source",
            "agent_activity",
            ["source_ref", "source_event_id"],
            unique=False,
        )

    checkpoint_indexes = {idx["name"] for idx in inspector.get_indexes("telemetry_checkpoints")}
    if "idx_telemetry_checkpoints_runtime" not in checkpoint_indexes:
        op.create_index(
            "idx_telemetry_checkpoints_runtime",
            "telemetry_checkpoints",
            ["runtime"],
            unique=False,
        )
    if "idx_telemetry_checkpoints_updated" not in checkpoint_indexes:
        op.create_index(
            "idx_telemetry_checkpoints_updated",
            "telemetry_checkpoints",
            ["updated_at"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("idx_telemetry_checkpoints_updated", table_name="telemetry_checkpoints")
    op.drop_index("idx_telemetry_checkpoints_runtime", table_name="telemetry_checkpoints")
    op.drop_table("telemetry_checkpoints")
    op.drop_index("idx_activity_source", table_name="agent_activity")
    op.drop_index("idx_activity_trace", table_name="agent_activity")
    op.drop_index("idx_activity_runtime_ts", table_name="agent_activity")
    op.drop_column("agent_activity", "model")
    op.drop_column("agent_activity", "parent_trace_id")
    op.drop_column("agent_activity", "trace_id")
    op.drop_column("agent_activity", "source_event_id")
    op.drop_column("agent_activity", "source_ref")
    op.drop_column("agent_activity", "source_kind")
    op.drop_column("agent_activity", "runtime")
    op.drop_column("agent_activity", "entity")
