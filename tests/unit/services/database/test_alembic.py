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
    "issue_dependencies",
    "message_queue",
    "settings",
    "swarms",
}

_EXPECTED_INDEXES = {
    "uq_events_delivery_id",
    "idx_events_received",
    "idx_events_repo",
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


async def test_restamp_rebuilds_indexes_and_collapses_duplicate_queue_rows(tmp_path):
    """A database whose indexes predate the model is repaired when it is re-stamped."""
    from sqlalchemy import text

    from agent_backbone.services.database.backbone_db import _repair_schema

    db = BackboneDB(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    await db.start()
    engine = db.engine
    try:
        async with engine.begin() as conn:
            # An older install: no dedup rule for PR notices, so the queue grew copies.
            await conn.execute(text("DROP INDEX uq_mq_message_dedup"))
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
            names = {
                row[0]
                for row in (
                    await conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
                ).fetchall()
            }
        assert statuses == ["pending", "expired", "expired"]
        assert "uq_mq_message_dedup" in names
        # ...and the rule now holds for new rows.
        assert (
            await db.queue.enqueue(
                session_name="ike", message="same notice", delivery_kind="pull_request"
            )
            == -1
        )
    finally:
        await db.stop()
