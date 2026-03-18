"""add swarm registry tables

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-13 16:08:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "swarms" not in inspector.get_table_names():
        op.create_table(
            "swarms",
            sa.Column("swarm_id", sa.Text(), nullable=False),
            sa.Column("repo", sa.Text(), nullable=False),
            sa.Column("task_id", sa.Text(), nullable=True),
            sa.Column("coding_agent_session", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("completed_at", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "status IN ('active', 'completing', 'completed', 'failed')",
                name="ck_swarms_status",
            ),
            sa.PrimaryKeyConstraint("swarm_id"),
        )

    if "swarm_workers" not in inspector.get_table_names():
        op.create_table(
            "swarm_workers",
            sa.Column("worker_id", sa.Text(), nullable=False),
            sa.Column("swarm_id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("branch", sa.Text(), nullable=False),
            sa.Column("worktree_path", sa.Text(), nullable=False),
            sa.Column("session", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("pr_number", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'started', 'working', 'pr_created', 'done', 'failed')",
                name="ck_swarm_workers_status",
            ),
            sa.ForeignKeyConstraint(["swarm_id"], ["swarms.swarm_id"]),
            sa.PrimaryKeyConstraint("worker_id"),
            sa.UniqueConstraint("swarm_id", "name", name="uq_swarm_workers_swarm_name"),
        )

    inspector = sa.inspect(conn)
    swarm_indexes = {idx["name"] for idx in inspector.get_indexes("swarms")}
    if "idx_swarms_repo" not in swarm_indexes:
        op.create_index("idx_swarms_repo", "swarms", ["repo"], unique=False)
    if "idx_swarms_status" not in swarm_indexes:
        op.create_index("idx_swarms_status", "swarms", ["status"], unique=False)
    if "idx_swarms_created" not in swarm_indexes:
        op.create_index("idx_swarms_created", "swarms", ["created_at"], unique=False)

    worker_indexes = {idx["name"] for idx in inspector.get_indexes("swarm_workers")}
    if "idx_swarm_workers_swarm" not in worker_indexes:
        op.create_index("idx_swarm_workers_swarm", "swarm_workers", ["swarm_id"], unique=False)
    if "idx_swarm_workers_session" not in worker_indexes:
        op.create_index(
            "idx_swarm_workers_session",
            "swarm_workers",
            ["session"],
            unique=False,
        )
    if "idx_swarm_workers_status" not in worker_indexes:
        op.create_index("idx_swarm_workers_status", "swarm_workers", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_swarm_workers_status", table_name="swarm_workers")
    op.drop_index("idx_swarm_workers_session", table_name="swarm_workers")
    op.drop_index("idx_swarm_workers_swarm", table_name="swarm_workers")
    op.drop_table("swarm_workers")
    op.drop_index("idx_swarms_created", table_name="swarms")
    op.drop_index("idx_swarms_status", table_name="swarms")
    op.drop_index("idx_swarms_repo", table_name="swarms")
    op.drop_table("swarms")
