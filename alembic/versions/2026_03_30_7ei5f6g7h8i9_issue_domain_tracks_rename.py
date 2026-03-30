"""add canonical issue projection and rename governance tables to tracks

Revision ID: 7ei5f6g7h8i9
Revises: 6dh4c5d6e7f8
Create Date: 2026-03-30 21:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7ei5f6g7h8i9"
down_revision: str | None = "6dh4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _dialect() -> str:
    return op.get_bind().dialect.name


def _rename_constraint(table: str, old_name: str, new_name: str) -> None:
    if _dialect() != "postgresql":
        return
    op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old_name}" TO "{new_name}"')


def _backfill_track_issue_refs() -> None:
    dialect = _dialect()
    if dialect == "postgresql":
        op.execute(
            """
            UPDATE track_instances
            SET repo_full_name = COALESCE(
                    context::json->>'repo_full_name',
                    context::json->>'repo',
                    repo_full_name
                ),
                issue_number = COALESCE(
                    NULLIF(context::json->>'issue_number', '')::integer,
                    NULLIF(context::json->>'issue_id', '')::integer,
                    issue_number
                )
            """
        )
        return
    if dialect == "sqlite":
        op.execute(
            """
            UPDATE track_instances
            SET repo_full_name = COALESCE(
                    json_extract(context, '$.repo_full_name'),
                    json_extract(context, '$.repo'),
                    repo_full_name
                ),
                issue_number = COALESCE(
                    CAST(json_extract(context, '$.issue_number') AS INTEGER),
                    CAST(json_extract(context, '$.issue_id') AS INTEGER),
                    issue_number
                )
            """
        )


def upgrade() -> None:
    op.create_table(
        "issues",
        sa.Column("repo_full_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="open"),
        sa.Column("html_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("user_login", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("labels_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("comment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("priority_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("synced_at", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("repo_full_name", "issue_number", name=op.f("pk_issues")),
    )
    op.create_index("idx_issues_repo", "issues", ["repo_full_name"])
    op.create_index("idx_issues_state", "issues", ["state"])
    op.create_index("idx_issues_priority", "issues", ["priority_score"])

    op.create_table(
        "issue_comments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("repo_full_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("user_login", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("from_entity", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_issue_comments")),
    )
    op.create_index("idx_comments_issue", "issue_comments", ["repo_full_name", "issue_number"])

    op.rename_table("governance_tracks", "tracks")
    op.rename_table("governance_track_instances", "track_instances")
    op.rename_table("governance_track_layouts", "track_layouts")

    op.drop_index("idx_governance_tracks_name", table_name="tracks")
    op.drop_index("idx_governance_instances_track", table_name="track_instances")
    op.drop_index("idx_governance_instances_state", table_name="track_instances")
    op.create_index("idx_tracks_name", "tracks", ["name"])
    op.create_index("idx_track_instances_track", "track_instances", ["track_id"])
    op.create_index("idx_track_instances_state", "track_instances", ["current_state"])

    op.add_column(
        "track_instances",
        sa.Column("repo_full_name", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "track_instances",
        sa.Column("issue_number", sa.Integer(), nullable=False, server_default="0"),
    )
    _backfill_track_issue_refs()
    op.create_index("idx_track_instances_issue", "track_instances", ["repo_full_name", "issue_number"])

    _rename_constraint("tracks", "pk_governance_tracks", "pk_tracks")
    _rename_constraint("track_instances", "pk_governance_track_instances", "pk_track_instances")
    _rename_constraint("track_layouts", "pk_governance_track_layouts", "pk_track_layouts")
    _rename_constraint(
        "track_instances",
        "fk_governance_track_instances_track_id_governance_tracks",
        "fk_track_instances_track_id_tracks",
    )


def downgrade() -> None:
    _rename_constraint("track_instances", "fk_track_instances_track_id_tracks", "fk_governance_track_instances_track_id_governance_tracks")
    _rename_constraint("track_layouts", "pk_track_layouts", "pk_governance_track_layouts")
    _rename_constraint("track_instances", "pk_track_instances", "pk_governance_track_instances")
    _rename_constraint("tracks", "pk_tracks", "pk_governance_tracks")

    op.drop_index("idx_track_instances_issue", table_name="track_instances")
    op.drop_column("track_instances", "issue_number")
    op.drop_column("track_instances", "repo_full_name")
    op.drop_index("idx_track_instances_state", table_name="track_instances")
    op.drop_index("idx_track_instances_track", table_name="track_instances")
    op.drop_index("idx_tracks_name", table_name="tracks")
    op.create_index("idx_governance_instances_state", "track_instances", ["current_state"])
    op.create_index("idx_governance_instances_track", "track_instances", ["track_id"])
    op.create_index("idx_governance_tracks_name", "tracks", ["name"])

    op.rename_table("track_layouts", "governance_track_layouts")
    op.rename_table("track_instances", "governance_track_instances")
    op.rename_table("tracks", "governance_tracks")

    op.drop_index("idx_comments_issue", table_name="issue_comments")
    op.drop_table("issue_comments")
    op.drop_index("idx_issues_priority", table_name="issues")
    op.drop_index("idx_issues_state", table_name="issues")
    op.drop_index("idx_issues_repo", table_name="issues")
    op.drop_table("issues")
