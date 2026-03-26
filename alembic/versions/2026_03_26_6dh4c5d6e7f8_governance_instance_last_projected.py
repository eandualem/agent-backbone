"""add last_projected_state column to governance track instances

Revision ID: 6dh4c5d6e7f8
Revises: 5cg3b4c5d6e7
Create Date: 2026-03-26 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6dh4c5d6e7f8"
down_revision: str | None = "5cg3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "governance_track_instances",
        sa.Column("last_projected_state", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("governance_track_instances", "last_projected_state")
