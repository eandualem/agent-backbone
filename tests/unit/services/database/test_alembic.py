"""Tests for the database schema and Alembic migration infrastructure."""

from __future__ import annotations

from pathlib import Path

from agent_backbone.services.database import build_engine
from agent_backbone.services.database.backbone_db import BackboneDB, metadata
from agent_backbone.services.database.config import DatabaseConfig

_EXPECTED_TABLES = {
    "acknowledgments",
    "agent_states",
    "dedup_log",
    "deliveries",
    "issue_dependencies",
    "message_queue",
}

_EXPECTED_INDEXES = {
    "idx_deliveries_issue",
    "idx_deliveries_entity",
    "idx_deliveries_outcome",
    "idx_deliveries_created",
    "uq_deliveries_active_owner",
    "idx_deps_sub",
    "idx_mq_leased",
    "idx_mq_status",
    "idx_mq_session",
    "uq_mq_comment_dedup",
    "uq_mq_dm_dedup",
    "uq_mq_issue_dedup",
}


def test_metadata_tables_match_expected():
    assert {t.name for t in metadata.sorted_tables} == _EXPECTED_TABLES


def test_metadata_indexes_match_expected():
    index_names = {idx.name for table in metadata.sorted_tables for idx in table.indexes}
    assert index_names == _EXPECTED_INDEXES


async def test_memory_db_bypasses_migrations():
    async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
        assert await db.check_connection()
        row_id = await db.record_delivery(1, "ike", "ike", "delivered")
        assert row_id > 0
        assert len(await db.query_deliveries(issue_number=1)) == 1


async def test_file_db_runs_migrations(tmp_path):
    """File-based BackboneDB creates the schema and alembic_version via start()."""
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    db = BackboneDB(engine)
    await db.start()
    try:
        assert await db.check_connection()
        assert await db.record_delivery(42, "ike", "ike", "delivered") > 0
    finally:
        db._engine = None
        await engine.dispose()


async def test_direct_migrations_bootstrap_fresh_persistent_db(tmp_path):
    """_run_migrations() upgrades a fresh persistent database from the initial revision."""
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    db = BackboneDB(engine)
    try:
        await db._run_migrations()
        assert await db.record_delivery(7, "ike", "ike", "delivered") > 0
        await db.enqueue_message("ike", "hello", delivery_kind="direct_message")
        assert (await db.get_message_by_id(1))["status"] == "pending"
    finally:
        db._engine = None
        await engine.dispose()


async def test_file_db_idempotent_start(tmp_path):
    db_path = tmp_path / "idem.db"
    engine = build_engine(f"sqlite+aiosqlite:///{db_path}")
    db = BackboneDB(engine)
    await db.start()
    await db.record_delivery(42, "ike", "ike", "delivered")
    db._engine = None
    await engine.dispose()

    engine2 = build_engine(f"sqlite+aiosqlite:///{db_path}")
    db2 = BackboneDB(engine2)
    await db2.start()
    try:
        assert len(await db2.query_deliveries(issue_number=42)) == 1
    finally:
        db2._engine = None
        await engine2.dispose()


def test_database_config_defaults_to_sqlite_in_data_dir(tmp_path):
    config = DatabaseConfig()
    assert config.resolved_url(tmp_path) == f"sqlite+aiosqlite:///{tmp_path / 'backbone.db'}"


def test_database_config_explicit_url_wins(tmp_path):
    url = "postgresql+asyncpg://u:p@db:5432/backbone"
    assert DatabaseConfig(url=url).resolved_url(tmp_path) == url


def test_build_engine_creates_parent_directory(tmp_path):
    target = tmp_path / "nested" / "dir" / "backbone.db"
    build_engine(f"sqlite+aiosqlite:///{target}")
    assert Path(target).parent.is_dir()
