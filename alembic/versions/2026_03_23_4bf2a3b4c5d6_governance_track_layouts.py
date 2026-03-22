"""add governance track layouts table

Revision ID: 4bf2a3b4c5d6
Revises: 3ae1f2a3b4c5
Create Date: 2026-03-23 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4bf2a3b4c5d6"
down_revision: str | None = "3ae1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "governance_track_layouts",
        sa.Column("track_id", sa.Text(), nullable=False),
        sa.Column("positions", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("track_id", name=op.f("pk_governance_track_layouts")),
    )


def downgrade() -> None:
    op.drop_table("governance_track_layouts")
