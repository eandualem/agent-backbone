"""add status column to governance track instances

Revision ID: 5cg3b4c5d6e7
Revises: 4bf2a3b4c5d6
Create Date: 2026-03-26 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5cg3b4c5d6e7"
down_revision: str | None = "4bf2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "governance_track_instances",
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
    )


def downgrade() -> None:
    op.drop_column("governance_track_instances", "status")
