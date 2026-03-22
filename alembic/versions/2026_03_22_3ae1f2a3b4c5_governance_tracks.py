"""add governance tracks and instances tables

Revision ID: 3ae1f2a3b4c5
Revises: 29d0e1f2a3b4
Create Date: 2026-03-22 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3ae1f2a3b4c5"
down_revision: str | None = "29d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "governance_tracks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_governance_tracks")),
    )
    op.create_index("idx_governance_tracks_name", "governance_tracks", ["name"])

    op.create_table(
        "governance_track_instances",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("track_id", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("current_state", sa.Text(), nullable=False),
        sa.Column("history", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["governance_tracks.id"],
            name=op.f("fk_governance_track_instances_track_id_governance_tracks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_governance_track_instances")),
    )
    op.create_index(
        "idx_governance_instances_track", "governance_track_instances", ["track_id"]
    )
    op.create_index(
        "idx_governance_instances_state", "governance_track_instances", ["current_state"]
    )


def downgrade() -> None:
    op.drop_index("idx_governance_instances_state", table_name="governance_track_instances")
    op.drop_index("idx_governance_instances_track", table_name="governance_track_instances")
    op.drop_table("governance_track_instances")
    op.drop_index("idx_governance_tracks_name", table_name="governance_tracks")
    op.drop_table("governance_tracks")
