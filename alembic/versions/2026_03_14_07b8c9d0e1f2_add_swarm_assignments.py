"""add swarm assignments

Revision ID: 07b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-03-14 12:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "07b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "swarm_assignments" not in inspector.get_table_names():
        op.create_table(
            "swarm_assignments",
            sa.Column("assignment_id", sa.Integer(), nullable=False, autoincrement=True),
            sa.Column("swarm_id", sa.Text(), nullable=False),
            sa.Column("worker_name", sa.Text(), nullable=False),
            sa.Column("assigned_by", sa.Text(), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("file_paths", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("completed_at", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "status IN ('active', 'completed', 'superseded', 'cancelled')",
                name="ck_swarm_assignments_status",
            ),
            sa.ForeignKeyConstraint(["swarm_id"], ["swarms.swarm_id"]),
            sa.PrimaryKeyConstraint("assignment_id"),
        )

    inspector = sa.inspect(conn)
    indexes = {index["name"] for index in inspector.get_indexes("swarm_assignments")}
    if "idx_swarm_assignments_swarm" not in indexes:
        op.create_index(
            "idx_swarm_assignments_swarm",
            "swarm_assignments",
            ["swarm_id"],
            unique=False,
        )
    if "idx_swarm_assignments_worker" not in indexes:
        op.create_index(
            "idx_swarm_assignments_worker",
            "swarm_assignments",
            ["worker_name"],
            unique=False,
        )
    if "idx_swarm_assignments_status" not in indexes:
        op.create_index(
            "idx_swarm_assignments_status",
            "swarm_assignments",
            ["status"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "swarm_assignments" not in inspector.get_table_names():
        return

    indexes = {index["name"] for index in inspector.get_indexes("swarm_assignments")}
    if "idx_swarm_assignments_status" in indexes:
        op.drop_index("idx_swarm_assignments_status", table_name="swarm_assignments")
    if "idx_swarm_assignments_worker" in indexes:
        op.drop_index("idx_swarm_assignments_worker", table_name="swarm_assignments")
    if "idx_swarm_assignments_swarm" in indexes:
        op.drop_index("idx_swarm_assignments_swarm", table_name="swarm_assignments")
    op.drop_table("swarm_assignments")
