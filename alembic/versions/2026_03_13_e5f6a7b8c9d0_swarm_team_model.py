"""swarm team model

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-13 19:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        _upgrade_sqlite(conn)
        return

    inspector = sa.inspect(conn)
    table_names = set(inspector.get_table_names())

    if "swarms" in table_names:
        swarm_columns = {column["name"] for column in inspector.get_columns("swarms")}
        swarm_indexes = {index["name"] for index in inspector.get_indexes("swarms")}
        swarm_checks = {check["name"] for check in inspector.get_check_constraints("swarms")}

        if "idx_swarms_status" in swarm_indexes:
            op.drop_index("idx_swarms_status", table_name="swarms")

        with op.batch_alter_table("swarms") as batch_op:
            if "status" in swarm_columns and "phase" not in swarm_columns:
                batch_op.alter_column(
                    "status",
                    new_column_name="phase",
                    existing_type=sa.Text(),
                    existing_nullable=False,
                )
            elif "phase" not in swarm_columns:
                batch_op.add_column(
                    sa.Column("phase", sa.Text(), nullable=False, server_default="created")
                )

            if "ck_swarms_ck_swarms_status" in swarm_checks:
                batch_op.drop_constraint("ck_swarms_status", type_="check")
            if "ck_swarms_ck_swarms_phase" in swarm_checks:
                batch_op.drop_constraint("ck_swarms_phase", type_="check")
            batch_op.create_check_constraint(
                "ck_swarms_phase",
                "phase IN ('created', 'planning', 'working', 'validating', 'pr_open', "
                "'awaiting_review', 'merged', 'cleaned_up', 'failed', 'discarded')",
            )

        op.execute(
            sa.text(
                """UPDATE swarms
                   SET phase = CASE phase
                       WHEN 'active' THEN 'working'
                       WHEN 'completing' THEN 'validating'
                       WHEN 'completed' THEN 'cleaned_up'
                       ELSE phase
                   END"""
            )
        )

        inspector = sa.inspect(conn)
        swarm_indexes = {index["name"] for index in inspector.get_indexes("swarms")}
        if "idx_swarms_phase" not in swarm_indexes:
            op.create_index("idx_swarms_phase", "swarms", ["phase"], unique=False)

    if "swarm_workers" in table_names:
        worker_columns = {column["name"] for column in inspector.get_columns("swarm_workers")}
        worker_indexes = {index["name"] for index in inspector.get_indexes("swarm_workers")}
        worker_checks = {
            check["name"] for check in inspector.get_check_constraints("swarm_workers")
        }

        with op.batch_alter_table("swarm_workers") as batch_op:
            if "role" not in worker_columns:
                batch_op.add_column(
                    sa.Column("role", sa.Text(), nullable=True, server_default="coder")
                )
            if "summary" not in worker_columns:
                batch_op.add_column(sa.Column("summary", sa.Text(), nullable=True))
            if "failure_reason" not in worker_columns:
                batch_op.add_column(sa.Column("failure_reason", sa.Text(), nullable=True))
            if "completed_at" not in worker_columns:
                batch_op.add_column(sa.Column("completed_at", sa.Text(), nullable=True))

        op.execute(
            sa.text(
                """UPDATE swarm_workers
                   SET role = CASE
                       WHEN session IN (
                           SELECT coding_agent_session
                           FROM swarms
                           WHERE swarms.swarm_id = swarm_workers.swarm_id
                       ) THEN 'lead'
                       ELSE 'coder'
                   END
                   WHERE role IS NULL OR role = ''"""
            )
        )
        op.execute(
            sa.text(
                """UPDATE swarm_workers
                   SET completed_at = updated_at
                   WHERE completed_at IS NULL AND status IN ('done', 'failed')"""
            )
        )

        with op.batch_alter_table("swarm_workers") as batch_op:
            batch_op.alter_column(
                "role",
                existing_type=sa.Text(),
                nullable=False,
                server_default=None,
            )
            if "ck_swarm_workers_ck_swarm_workers_role" in worker_checks:
                batch_op.drop_constraint(
                    "ck_swarm_workers_role",
                    type_="check",
                )
            if "ck_swarm_workers_ck_swarm_workers_status" in worker_checks:
                batch_op.drop_constraint(
                    "ck_swarm_workers_status",
                    type_="check",
                )
            batch_op.create_check_constraint(
                "ck_swarm_workers_role",
                "role IN ('lead', 'coder', 'tester', 'validator', 'scout')",
            )
            batch_op.create_check_constraint(
                "ck_swarm_workers_status",
                "status IN ('pending', 'started', 'working', 'pr_created', 'done', 'failed')",
            )

        inspector = sa.inspect(conn)
        worker_indexes = {index["name"] for index in inspector.get_indexes("swarm_workers")}
        if "idx_swarm_workers_role" not in worker_indexes:
            op.create_index("idx_swarm_workers_role", "swarm_workers", ["role"], unique=False)

    inspector = sa.inspect(conn)
    table_names = set(inspector.get_table_names())

    if "swarm_phase_history" not in table_names:
        op.create_table(
            "swarm_phase_history",
            sa.Column("history_id", sa.Integer(), nullable=False, autoincrement=True),
            sa.Column("swarm_id", sa.Text(), nullable=False),
            sa.Column("from_phase", sa.Text(), nullable=True),
            sa.Column("to_phase", sa.Text(), nullable=False),
            sa.Column("timestamp", sa.Text(), nullable=False),
            sa.Column("triggered_by", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(["swarm_id"], ["swarms.swarm_id"]),
            sa.PrimaryKeyConstraint("history_id"),
        )
    phase_history_indexes = {
        index["name"] for index in sa.inspect(conn).get_indexes("swarm_phase_history")
    }
    if "idx_swarm_phase_history_swarm" not in phase_history_indexes:
        op.create_index(
            "idx_swarm_phase_history_swarm",
            "swarm_phase_history",
            ["swarm_id"],
            unique=False,
        )
    if "idx_swarm_phase_history_timestamp" not in phase_history_indexes:
        op.create_index(
            "idx_swarm_phase_history_timestamp",
            "swarm_phase_history",
            ["timestamp"],
            unique=False,
        )

    if "swarm_messages" not in table_names:
        op.create_table(
            "swarm_messages",
            sa.Column("message_id", sa.Integer(), nullable=False, autoincrement=True),
            sa.Column("swarm_id", sa.Text(), nullable=False),
            sa.Column("target_kind", sa.Text(), nullable=False),
            sa.Column("target_role", sa.Text(), nullable=True),
            sa.Column("target_worker_name", sa.Text(), nullable=True),
            sa.Column("from_entity", sa.Text(), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("delivered", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "target_kind IN ('broadcast', 'role', 'worker')",
                name="ck_swarm_messages_target_kind",
            ),
            sa.ForeignKeyConstraint(["swarm_id"], ["swarms.swarm_id"]),
            sa.PrimaryKeyConstraint("message_id"),
        )
    message_indexes = {index["name"] for index in sa.inspect(conn).get_indexes("swarm_messages")}
    if "idx_swarm_messages_swarm" not in message_indexes:
        op.create_index("idx_swarm_messages_swarm", "swarm_messages", ["swarm_id"], unique=False)
    if "idx_swarm_messages_created" not in message_indexes:
        op.create_index(
            "idx_swarm_messages_created",
            "swarm_messages",
            ["created_at"],
            unique=False,
        )

    op.execute(
        sa.text(
            """INSERT INTO swarm_phase_history
               (swarm_id, from_phase, to_phase, timestamp, triggered_by)
               SELECT s.swarm_id, NULL, s.phase, COALESCE(s.completed_at, s.created_at), 'migration'
               FROM swarms s
               WHERE NOT EXISTS (
                   SELECT 1
                   FROM swarm_phase_history h
                   WHERE h.swarm_id = s.swarm_id
               )"""
        )
    )


def _upgrade_sqlite(conn) -> None:
    inspector = sa.inspect(conn)
    table_names = set(inspector.get_table_names())

    op.execute(sa.text("PRAGMA foreign_keys=OFF"))

    if "swarms" in table_names:
        op.create_table(
            "_swarms_new",
            sa.Column("swarm_id", sa.Text(), nullable=False),
            sa.Column("repo", sa.Text(), nullable=False),
            sa.Column("task_id", sa.Text(), nullable=True),
            sa.Column("coding_agent_session", sa.Text(), nullable=False),
            sa.Column("phase", sa.Text(), nullable=False, server_default="created"),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("completed_at", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "phase IN ('created', 'planning', 'working', 'validating', 'pr_open', "
                "'awaiting_review', 'merged', 'cleaned_up', 'failed', 'discarded')",
                name="ck_swarms_phase",
            ),
            sa.PrimaryKeyConstraint("swarm_id"),
        )
        op.execute(
            sa.text(
                """INSERT INTO _swarms_new
                   (swarm_id, repo, task_id, coding_agent_session, phase, created_at, completed_at)
                   SELECT
                       swarm_id,
                       repo,
                       task_id,
                       coding_agent_session,
                       CASE status
                           WHEN 'active' THEN 'working'
                           WHEN 'completing' THEN 'validating'
                           WHEN 'completed' THEN 'cleaned_up'
                           ELSE status
                       END,
                       created_at,
                       completed_at
                   FROM swarms"""
            )
        )

    if "swarm_workers" in table_names:
        op.create_table(
            "_swarm_workers_new",
            sa.Column("worker_id", sa.Text(), nullable=False),
            sa.Column("swarm_id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("role", sa.Text(), nullable=False),
            sa.Column("branch", sa.Text(), nullable=False),
            sa.Column("worktree_path", sa.Text(), nullable=False),
            sa.Column("session", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("pr_number", sa.Integer(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("failure_reason", sa.Text(), nullable=True),
            sa.Column("completed_at", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "role IN ('lead', 'coder', 'tester', 'validator', 'scout')",
                name="ck_swarm_workers_role",
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'started', 'working', 'pr_created', 'done', 'failed')",
                name="ck_swarm_workers_status",
            ),
            sa.ForeignKeyConstraint(["swarm_id"], ["_swarms_new.swarm_id"]),
            sa.PrimaryKeyConstraint("worker_id"),
            sa.UniqueConstraint("swarm_id", "name", name="uq_swarm_workers_swarm_name"),
        )
        op.execute(
            sa.text(
                """INSERT INTO _swarm_workers_new
                   (worker_id, swarm_id, name, role, branch, worktree_path, session,
                    status, pr_number, summary, failure_reason, completed_at,
                    created_at, updated_at)
                   SELECT
                       w.worker_id,
                       w.swarm_id,
                       w.name,
                       CASE
                           WHEN w.session = s.coding_agent_session THEN 'lead'
                           ELSE 'coder'
                       END,
                       w.branch,
                       w.worktree_path,
                       w.session,
                       w.status,
                       w.pr_number,
                       NULL,
                       NULL,
                       CASE
                           WHEN w.status IN ('done', 'failed') THEN w.updated_at
                           ELSE NULL
                       END,
                       w.created_at,
                       w.updated_at
                   FROM swarm_workers w
                   JOIN swarms s ON s.swarm_id = w.swarm_id"""
            )
        )
        op.drop_table("swarm_workers")

    if "swarms" in table_names:
        op.drop_table("swarms")
        op.rename_table("_swarms_new", "swarms")

    if "swarm_workers" in table_names:
        op.rename_table("_swarm_workers_new", "swarm_workers")

    op.create_index("idx_swarms_repo", "swarms", ["repo"], unique=False)
    op.create_index("idx_swarms_phase", "swarms", ["phase"], unique=False)
    op.create_index("idx_swarms_created", "swarms", ["created_at"], unique=False)

    op.create_index("idx_swarm_workers_swarm", "swarm_workers", ["swarm_id"], unique=False)
    op.create_index("idx_swarm_workers_role", "swarm_workers", ["role"], unique=False)
    op.create_index("idx_swarm_workers_session", "swarm_workers", ["session"], unique=False)
    op.create_index("idx_swarm_workers_status", "swarm_workers", ["status"], unique=False)

    op.create_table(
        "swarm_phase_history",
        sa.Column("history_id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("swarm_id", sa.Text(), nullable=False),
        sa.Column("from_phase", sa.Text(), nullable=True),
        sa.Column("to_phase", sa.Text(), nullable=False),
        sa.Column("timestamp", sa.Text(), nullable=False),
        sa.Column("triggered_by", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["swarm_id"], ["swarms.swarm_id"]),
        sa.PrimaryKeyConstraint("history_id"),
    )
    op.create_index(
        "idx_swarm_phase_history_swarm",
        "swarm_phase_history",
        ["swarm_id"],
        unique=False,
    )
    op.create_index(
        "idx_swarm_phase_history_timestamp",
        "swarm_phase_history",
        ["timestamp"],
        unique=False,
    )

    op.create_table(
        "swarm_messages",
        sa.Column("message_id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("swarm_id", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_role", sa.Text(), nullable=True),
        sa.Column("target_worker_name", sa.Text(), nullable=True),
        sa.Column("from_entity", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("delivered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "target_kind IN ('broadcast', 'role', 'worker')",
            name="ck_swarm_messages_target_kind",
        ),
        sa.ForeignKeyConstraint(["swarm_id"], ["swarms.swarm_id"]),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index("idx_swarm_messages_swarm", "swarm_messages", ["swarm_id"], unique=False)
    op.create_index(
        "idx_swarm_messages_created",
        "swarm_messages",
        ["created_at"],
        unique=False,
    )

    op.execute(
        sa.text(
            """INSERT INTO swarm_phase_history
               (swarm_id, from_phase, to_phase, timestamp, triggered_by)
               SELECT swarm_id, NULL, phase, COALESCE(completed_at, created_at), 'migration'
               FROM swarms"""
        )
    )

    op.execute(sa.text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    op.drop_index("idx_swarm_messages_created", table_name="swarm_messages")
    op.drop_index("idx_swarm_messages_swarm", table_name="swarm_messages")
    op.drop_table("swarm_messages")

    op.drop_index("idx_swarm_phase_history_timestamp", table_name="swarm_phase_history")
    op.drop_index("idx_swarm_phase_history_swarm", table_name="swarm_phase_history")
    op.drop_table("swarm_phase_history")

    with op.batch_alter_table("swarm_workers") as batch_op:
        try:
            batch_op.drop_constraint("ck_swarm_workers_role", type_="check")
        except Exception:
            pass
        batch_op.drop_column("completed_at")
        batch_op.drop_column("failure_reason")
        batch_op.drop_column("summary")
        batch_op.drop_column("role")

    try:
        op.drop_index("idx_swarm_workers_role", table_name="swarm_workers")
    except Exception:
        pass

    try:
        op.drop_index("idx_swarms_phase", table_name="swarms")
    except Exception:
        pass

    with op.batch_alter_table("swarms") as batch_op:
        try:
            batch_op.drop_constraint("ck_swarms_phase", type_="check")
        except Exception:
            pass
        batch_op.alter_column(
            "phase",
            new_column_name="status",
            existing_type=sa.Text(),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_swarms_status",
            "status IN ('active', 'completing', 'completed', 'failed')",
        )

    op.execute(
        sa.text(
            """UPDATE swarms
               SET status = CASE status
                   WHEN 'working' THEN 'active'
                   WHEN 'validating' THEN 'completing'
                   WHEN 'cleaned_up' THEN 'completed'
                   ELSE status
               END"""
        )
    )
    op.create_index("idx_swarms_status", "swarms", ["status"], unique=False)
