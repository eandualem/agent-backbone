"""Tests for Alembic migration infrastructure."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine

from agent_backbone.services.database.backbone_db import BackboneDB, metadata

_EXPECTED_TABLES = {
    "acknowledgments",
    "agent_activity",
    "agent_states",
    "dedup_log",
    "deliveries",
    "heartbeats",
    "issue_dependencies",
    "message_queue",
    "swarms",
    "swarm_workers",
    "telemetry_checkpoints",
}

_EXPECTED_INDEXES = {
    "idx_activity_session_ts",
    "idx_activity_runtime_ts",
    "idx_activity_source",
    "idx_activity_trace",
    "idx_deliveries_issue",
    "idx_deliveries_entity",
    "idx_deliveries_outcome",
    "idx_deliveries_created",
    "idx_deps_sub",
    "idx_heartbeats_agent",
    "idx_mq_status",
    "idx_mq_session",
    "idx_swarms_created",
    "idx_swarms_repo",
    "idx_swarms_status",
    "idx_swarm_workers_session",
    "idx_swarm_workers_status",
    "idx_swarm_workers_swarm",
    "idx_telemetry_checkpoints_runtime",
    "idx_telemetry_checkpoints_updated",
}


# ---------------------------------------------------------------------------
# MetaData correctness
# ---------------------------------------------------------------------------


def test_metadata_tables_match_expected():
    """MetaData defines all expected tables."""
    table_names = {t.name for t in metadata.sorted_tables}
    assert table_names == _EXPECTED_TABLES


def test_metadata_indexes_match_expected():
    """MetaData defines all expected indexes."""
    index_names: set[str] = set()
    for table in metadata.sorted_tables:
        for idx in table.indexes:
            index_names.add(idx.name)
    assert index_names == _EXPECTED_INDEXES


# ---------------------------------------------------------------------------
# :memory: databases (bypass Alembic)
# ---------------------------------------------------------------------------


async def test_memory_db_bypasses_migrations():
    """:memory: BackboneDB works without Alembic (no alembic_version table)."""
    async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
        # Verify tables via check_connection
        assert await db.check_connection()

        # Verify we can do basic operations
        row_id = await db.record_delivery(
            issue_number=1,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
        )
        assert row_id > 0


async def test_memory_db_functional():
    """:memory: DB supports basic operations after schema init."""
    async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
        row_id = await db.record_delivery(
            issue_number=1,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
        )
        assert row_id > 0

        records = await db.query_deliveries(issue_number=1)
        assert len(records) == 1


# ---------------------------------------------------------------------------
# File-based databases (Alembic migrations)
# ---------------------------------------------------------------------------


async def test_file_db_runs_migrations(tmp_path):
    """File-based BackboneDB creates alembic_version via start()."""
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    db = BackboneDB(engine)
    await db.start()

    try:
        assert await db.check_connection()

        # Verify we can do basic operations (schema is correct)
        row_id = await db.record_delivery(
            issue_number=42,
            target_entity="ike",
            session_name="ike",
            outcome="delivered",
        )
        assert row_id > 0
    finally:
        db._engine = None
        await engine.dispose()


async def test_file_db_idempotent_start(tmp_path):
    """Calling start() twice on a file DB is safe (idempotent)."""
    db_path = tmp_path / "idem.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    db = BackboneDB(engine)
    await db.start()

    # Record something
    await db.record_delivery(
        issue_number=42,
        target_entity="ike",
        session_name="ike",
        outcome="delivered",
    )
    db._engine = None
    await engine.dispose()

    # Second start — should not fail or lose data
    engine2 = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    db2 = BackboneDB(engine2)
    await db2.start()

    try:
        records = await db2.query_deliveries(issue_number=42)
        assert len(records) == 1
    finally:
        db2._engine = None
        await engine2.dispose()
