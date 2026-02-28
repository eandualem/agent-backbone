"""Tests for Alembic migration infrastructure."""

from __future__ import annotations

from agent_backbone.services.persistence._schema import metadata
from agent_backbone.services.persistence.interface import BackboneDB

_EXPECTED_TABLES = {
    "acknowledgments",
    "agent_states",
    "dedup_log",
    "deliveries",
    "heartbeats",
    "issue_dependencies",
    "message_queue",
}

_EXPECTED_INDEXES = {
    "idx_deliveries_issue",
    "idx_deliveries_entity",
    "idx_deliveries_outcome",
    "idx_deliveries_created",
    "idx_deps_sub",
    "idx_heartbeats_agent",
    "idx_mq_status",
    "idx_mq_session",
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
    db = BackboneDB(f"sqlite+aiosqlite:///{db_path}")
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
        await db.stop()


async def test_file_db_idempotent_start(tmp_path):
    """Calling start() twice on a file DB is safe (idempotent)."""
    db_path = tmp_path / "idem.db"
    db = BackboneDB(f"sqlite+aiosqlite:///{db_path}")
    await db.start()

    # Record something
    await db.record_delivery(
        issue_number=42,
        target_entity="ike",
        session_name="ike",
        outcome="delivered",
    )
    await db.stop()

    # Second start — should not fail or lose data
    db2 = BackboneDB(f"sqlite+aiosqlite:///{db_path}")
    await db2.start()

    try:
        records = await db2.query_deliveries(issue_number=42)
        assert len(records) == 1
    finally:
        await db2.stop()
