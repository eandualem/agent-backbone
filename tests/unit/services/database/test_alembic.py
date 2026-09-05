"""Tests for the database schema and Alembic migration infrastructure."""

from __future__ import annotations

import hashlib
from pathlib import Path

from agent_backbone.config import sqlite_url
from agent_backbone.services.database import build_engine
from agent_backbone.services.database.backbone_db import BackboneDB, metadata
from tests.support import queue_row

_EXPECTED_TABLES = {
    "acknowledgments",
    "agent_states",
    "agent_watches",
    "agents",
    "deliveries",
    "events",
    "event_outbox",
    "issue_dependencies",
    "message_queue",
    "poll_cursors",
    "settings",
    "swarms",
}

_EXPECTED_INDEXES = {
    "uq_events_delivery_id",
    "idx_events_received",
    "idx_events_repo",
    "idx_outbox_pending",
    "idx_deliveries_issue",
    "idx_deliveries_entity",
    "idx_deliveries_outcome",
    "idx_deliveries_created",
    "uq_deliveries_active_owner",
    "idx_deps_sub",
    "idx_mq_leased",
    "idx_mq_status",
    "idx_mq_session",
    "uq_mq_issue_dedup",
    "uq_mq_message_dedup",
    "uq_swarms_active_issue",
}


def test_metadata_tables_match_expected():
    assert {t.name for t in metadata.sorted_tables} == _EXPECTED_TABLES


def test_metadata_indexes_match_expected():
    index_names = {idx.name for table in metadata.sorted_tables for idx in table.indexes}
    assert index_names == _EXPECTED_INDEXES


async def test_memory_db_bypasses_migrations():
    async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
        assert await db.check_connection()
        row_id = await db.deliveries.record(
            issue_number=1, target_entity="ike", session_name="ike", outcome="delivered"
        )
        assert row_id > 0
        assert len(await db.deliveries.query(issue_number=1)) == 1


async def test_file_db_runs_migrations(tmp_path):
    """File-based BackboneDB creates the schema and alembic_version via start()."""
    db = BackboneDB(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await db.start()
    try:
        assert await db.check_connection()
        assert (
            await db.deliveries.record(
                issue_number=42, target_entity="ike", session_name="ike", outcome="delivered"
            )
            > 0
        )
    finally:
        await db.stop()


async def test_direct_migrations_bootstrap_fresh_persistent_db(tmp_path):
    """_run_migrations() upgrades a fresh persistent database from the initial revision."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}"
    db = BackboneDB(url)
    db._engine = build_engine(url)  # migrations alone, without start()'s create_all
    try:
        await db._run_migrations()
        assert (
            await db.deliveries.record(
                issue_number=7, target_entity="ike", session_name="ike", outcome="delivered"
            )
            > 0
        )
        await db.queue.enqueue(session_name="ike", message="hello", delivery_kind="direct_message")
        assert (await queue_row(db, 1))["status"] == "pending"
    finally:
        await db.stop()


async def test_file_db_idempotent_start(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'idem.db'}"
    db = BackboneDB(url)
    await db.start()
    await db.deliveries.record(
        issue_number=42, target_entity="ike", session_name="ike", outcome="delivered"
    )
    await db.stop()

    db2 = BackboneDB(url)
    await db2.start()
    try:
        assert len(await db2.deliveries.query(issue_number=42)) == 1
    finally:
        await db2.stop()


def test_sqlite_url_points_into_data_dir(tmp_path):
    assert sqlite_url(tmp_path) == f"sqlite+aiosqlite:///{tmp_path / 'backbone.db'}"


def test_build_engine_creates_parent_directory(tmp_path):
    target = tmp_path / "nested" / "dir" / "backbone.db"
    build_engine(f"sqlite+aiosqlite:///{target}")
    assert Path(target).parent.is_dir()


async def test_unknown_stamped_revision_is_restamped_after_squash(tmp_path):
    """Pre-1.0 squash policy: a DB stamped with a revision that no longer
    exists (the squash was regenerated) must re-stamp to head, not crash."""
    from sqlalchemy import text

    url = f"sqlite+aiosqlite:///{tmp_path}/old.db"
    db = BackboneDB(url)
    await db.start()  # creates schema and stamps head
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = 'deadbeef0000'"))
    await db.stop()

    db2 = BackboneDB(url)
    await db2.start()  # must not raise
    async with db2.engine.begin() as conn:
        stored = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    assert stored != "deadbeef0000"
    await db2.stop()


async def test_old_sqlite_schema_gains_cursor_table_on_restart(tmp_path):
    from sqlalchemy import text

    url = f"sqlite+aiosqlite:///{tmp_path / 'before-cursors.db'}"
    async with BackboneDB.connect(url) as db:
        await db.events.record(delivery_id="kept", source="poll", event_type="test")
        async with db.engine.begin() as conn:
            await conn.execute(text("DROP TABLE poll_cursors"))
            await conn.execute(text("UPDATE alembic_version SET version_num='pre_cursor_squash'"))
    async with BackboneDB.connect(url) as db:
        await db.events.save_poll_cursor("acme/a", "2026-01-01T00:00:00Z")
        assert await db.events.poll_cursor("acme/a") == "2026-01-01T00:00:00Z"
        assert len(await db.events.query()) == 1


async def test_old_schema_gains_new_tables_without_startup_create_all(tmp_path):
    """The PostgreSQL migration path: no SQLite startup create_all to fill gaps."""
    from sqlalchemy import text

    url = f"sqlite+aiosqlite:///{tmp_path / 'old-direct.db'}"
    async with BackboneDB.connect(url) as db:
        await db.events.record(delivery_id="preserved", source="poll", event_type="test")
        async with db.engine.begin() as conn:
            await conn.execute(text("DROP TABLE poll_cursors"))
            await conn.execute(text("UPDATE alembic_version SET version_num='old_squash'"))
        # Invoke migrations directly, bypassing start() and its SQLite shortcut.
        await db._run_migrations()
        await db.events.save_poll_cursor("acme/a", "2026-01-01T00:00:00Z")
        assert await db.events.poll_cursor("acme/a") == "2026-01-01T00:00:00Z"
        assert (await db.events.query())[0]["delivery_id"] == "preserved"
        async with db.engine.connect() as conn:
            stamp = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        assert stamp != "old_squash"
        # The repaired schema remains valid and idempotent on another run.
        await db._run_migrations()


async def test_restamp_rebuilds_indexes_and_collapses_duplicate_queue_rows(tmp_path):
    """A database whose queue predates ``dedup_key`` is repaired when it is re-stamped."""
    from sqlalchemy import inspect, text

    from agent_backbone.services.database.backbone_db import _repair_schema

    db = BackboneDB(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    await db.start()
    engine = db.engine
    try:
        async with engine.begin() as conn:
            # An older install: content_hash instead of dedup_key, no dedup
            # rule for PR notices, so the queue grew copies.
            await conn.execute(text("DROP INDEX uq_mq_message_dedup"))
            await conn.execute(text("ALTER TABLE message_queue ADD COLUMN content_hash TEXT"))
            digest = hashlib.sha256(b"same notice").hexdigest()
            for _ in range(3):
                await conn.execute(
                    text(
                        "INSERT INTO message_queue (session_name, message, delivery_kind, "
                        "enqueued_at, status, content_hash) VALUES "
                        "('ike', 'same notice', 'pull_request', '2026-09-01T00:00:00.000000Z', "
                        "'pending', :digest)"
                    ),
                    {"digest": digest},
                )
        async with engine.begin() as conn:
            await conn.run_sync(_repair_schema)
            statuses = (
                (await conn.execute(text("SELECT status FROM message_queue ORDER BY id")))
                .scalars()
                .all()
            )
            keys = (await conn.execute(text("SELECT dedup_key FROM message_queue"))).scalars().all()
            names = {
                row[0]
                for row in (
                    await conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
                ).fetchall()
            }
            columns = {
                c["name"]
                for c in await conn.run_sync(lambda s: inspect(s).get_columns("message_queue"))
            }
        assert statuses == ["pending", "expired", "expired"]
        assert len(set(keys)) == 1 and keys[0].startswith("msg:")
        assert "uq_mq_message_dedup" in names
        assert "content_hash" not in columns and "dedup_key" in columns
        # ...and the rule now holds for new rows: the old pending copy is *the same* message.
        again = await db.queue.enqueue(
            session_name="ike", message="same notice", delivery_kind="pull_request"
        )
        assert again.status == "already_queued"
    finally:
        await db.stop()


async def test_restamp_drops_a_column_the_old_index_still_names(tmp_path):
    """The 0.1.0 database: uq_mq_message_dedup on content_hash is present when
    content_hash has to go. SQLite refuses DROP COLUMN while an index names
    the column, so the repair must drop indexes before columns (seen live)."""
    from sqlalchemy import inspect, text

    from agent_backbone.services.database.backbone_db import _repair_schema

    db = BackboneDB(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    await db.start()
    try:
        async with db.engine.begin() as conn:
            await conn.execute(text("DROP INDEX uq_mq_message_dedup"))
            await conn.execute(text("ALTER TABLE message_queue ADD COLUMN content_hash TEXT"))
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_mq_message_dedup ON message_queue "
                    "(session_name, content_hash) WHERE delivery_kind != 'issue' "
                    "AND status IN ('pending','in_progress')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO message_queue (session_name, message, delivery_kind, "
                    "enqueued_at, status, content_hash) VALUES "
                    "('ike', 'old notice', 'watch', '2026-09-01T00:00:00.000000Z', 'pending', 'h')"
                )
            )
        async with db.engine.begin() as conn:
            await conn.run_sync(_repair_schema)
            columns = {
                c["name"]
                for c in await conn.run_sync(lambda s: inspect(s).get_columns("message_queue"))
            }
            key = (await conn.execute(text("SELECT dedup_key FROM message_queue"))).scalar()
        assert "content_hash" not in columns and "dedup_key" in columns
        assert key.startswith("msg:")
    finally:
        await db.stop()


async def test_restamp_migrates_the_previous_squash_columns(tmp_path):
    """A database from the 387112cb1193 squash has flow_name/flow_run_id, not source."""
    from sqlalchemy import inspect, text

    db = BackboneDB(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    await db.start()
    try:
        async with db.engine.begin() as conn:
            for table in ("deliveries", "message_queue"):
                await conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN source TO flow_name"))
            await conn.execute(
                text("ALTER TABLE deliveries ADD COLUMN flow_run_id TEXT NOT NULL DEFAULT ''")
            )
            await conn.execute(
                text(
                    "INSERT INTO deliveries (kind, repo, issue_number, target_entity, "
                    "session_name, outcome, flow_name, flow_run_id, preview, created_at) VALUES "
                    "('issue', 'acme/app', 1, 'ike', 'ike', 'delivered', 'issue-dispatcher', '', "
                    "'', '2026-09-01T00:00:00.000000Z')"
                )
            )
            await conn.execute(text("UPDATE alembic_version SET version_num = '387112cb1193'"))
    finally:
        await db.stop()

    db2 = BackboneDB(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    await db2.start()
    try:
        async with db2.engine.connect() as conn:
            columns = {
                table: {
                    c["name"]
                    for c in await conn.run_sync(lambda sync, t=table: inspect(sync).get_columns(t))
                }
                for table in ("deliveries", "message_queue")
            }
        assert "source" in columns["deliveries"] and "flow_name" not in columns["deliveries"]
        assert "flow_run_id" not in columns["deliveries"]
        assert "source" in columns["message_queue"]
        (row,) = await db2.deliveries.query(issue_number=1)
        assert row["source"] == "issue-dispatcher"
        # and new writes work
        await db2.deliveries.record(
            issue_number=2, target_entity="ike", session_name="ike", outcome="delivered", source="x"
        )
    finally:
        await db2.stop()


async def test_restamp_on_old_sqlite_keeps_the_dead_column(tmp_path, monkeypatch):
    """SQLite before 3.35 cannot DROP COLUMN: the start must still succeed."""
    import sqlite3

    from sqlalchemy import inspect, text

    url = f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"
    db = BackboneDB(url)
    await db.start()
    try:
        async with db.engine.begin() as conn:
            await conn.execute(text("ALTER TABLE deliveries RENAME COLUMN source TO flow_name"))
            await conn.execute(
                text("ALTER TABLE deliveries ADD COLUMN flow_run_id TEXT NOT NULL DEFAULT ''")
            )
            await conn.execute(text("UPDATE alembic_version SET version_num = '387112cb1193'"))
    finally:
        await db.stop()

    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 34, 0))
    db2 = BackboneDB(url)
    await db2.start()  # must not raise
    try:
        async with db2.engine.connect() as conn:
            columns = {
                c["name"]
                for c in await conn.run_sync(lambda s: inspect(s).get_columns("deliveries"))
            }
        assert "source" in columns and "flow_run_id" in columns  # renamed, dead one kept
        await db2.deliveries.record(
            issue_number=1, target_entity="ike", session_name="ike", outcome="delivered", source="x"
        )
    finally:
        await db2.stop()
