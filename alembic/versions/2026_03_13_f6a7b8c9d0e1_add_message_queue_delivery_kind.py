"""Add delivery kind to queued messages.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-03-13 22:50:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("message_queue") as batch_op:
        batch_op.add_column(
            sa.Column(
                "delivery_kind",
                sa.Text(),
                nullable=False,
                server_default="issue",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("message_queue") as batch_op:
        batch_op.drop_column("delivery_kind")
