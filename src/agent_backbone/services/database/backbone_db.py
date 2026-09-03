"""Database persistence — engine lifecycle, migrations and query delegation."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import agent_backbone.services.database.models  # noqa: F401  (registers the ORM tables)
from agent_backbone.services.database._acks_repo import AcknowledgementRepo
from agent_backbone.services.database._agents_repo import AgentRepo
from agent_backbone.services.database._delivery_repo import DeliveryRepo
from agent_backbone.services.database._dependencies_repo import DependencyRepo
from agent_backbone.services.database._events_repo import EventRepo
from agent_backbone.services.database._queue_repo import QueueRepo
from agent_backbone.services.database._settings_repo import SettingRepo
from agent_backbone.services.database._state_repo import StateRepo
from agent_backbone.services.database._swarms_repo import SwarmRepo
from agent_backbone.services.database.base import Base
from agent_backbone.services.database.engine import build_engine, redact_url

metadata = Base.metadata

log = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
"""Migrations ship inside the package so installed CLIs migrate from anywhere."""


_SUPERSEDED_INDEXES = ("uq_mq_comment_dedup", "uq_mq_dm_dedup")
"""Indexes earlier releases created that the model no longer has. Only these
and the model's own are ever dropped — an index an operator added is theirs."""

_RENAMED_COLUMNS = {"deliveries": {"flow_name": "source"}, "message_queue": {"flow_name": "source"}}
"""Columns the previous squash spelled differently: renamed in place, data kept."""
_DROPPED_COLUMNS = {"deliveries": ("flow_run_id",), "message_queue": ("content_hash",)}
"""Columns the previous squash had and the model no longer has."""


def _repair_columns(sync_conn) -> None:
    """Rename, add and drop columns so a re-stamped database matches the model.

    PostgreSQL supports all three statements. SQLite gained ``RENAME COLUMN``
    in 3.25 and ``DROP COLUMN`` in 3.35; on an older system library the
    rename falls back to add-and-copy and the dead column is left in place
    (nullable and unused) rather than failing the start. Column *types* and
    nullability are never altered.
    """
    from sqlalchemy import inspect

    sqlite_version = _sqlite_version(sync_conn)
    inspector = inspect(sync_conn)
    for table in metadata.sorted_tables:
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for old, new in _RENAMED_COLUMNS.get(table.name, {}).items():
            if old in existing and new not in existing:
                if sqlite_version is None or sqlite_version >= (3, 25):
                    sync_conn.execute(
                        text(f"ALTER TABLE {table.name} RENAME COLUMN {old} TO {new}")
                    )
                    existing.discard(old)
                else:  # SQLite < 3.25: add the new column and copy the values across
                    sync_conn.execute(
                        text(f"ALTER TABLE {table.name} ADD COLUMN {new} TEXT NOT NULL DEFAULT ''")
                    )
                    sync_conn.execute(text(f"UPDATE {table.name} SET {new} = {old}"))
                sync_conn.execute(text(f"UPDATE {table.name} SET {new} = '' WHERE {new} IS NULL"))
                existing.add(new)
                log.info("Renamed %s.%s to %s (re-stamped database)", table.name, old, new)
        for column in table.columns:
            if column.name in existing:
                continue
            default = column.server_default.arg if column.server_default is not None else None
            clause = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type}"
            if default is not None:
                clause += f" NOT NULL DEFAULT '{default}'"
            sync_conn.execute(text(clause))
            log.info("Added %s.%s (re-stamped database)", table.name, column.name)
        for old in _DROPPED_COLUMNS.get(table.name, ()):
            if old not in existing:
                continue
            if sqlite_version is not None and sqlite_version < (3, 35):
                log.info(
                    "Leaving %s.%s in place: SQLite %s cannot drop columns (3.35 needed)",
                    table.name,
                    old,
                    ".".join(map(str, sqlite_version)),
                )
                continue
            sync_conn.execute(text(f"ALTER TABLE {table.name} DROP COLUMN {old}"))
            log.info("Dropped %s.%s (re-stamped database)", table.name, old)


def _backfill_dedup_keys(sync_conn) -> None:
    """Give queue rows from before ``dedup_key`` (they had ``content_hash``)
    the identity new rows get, so the dedup index can be built and a
    re-offered message still folds into the row that already waits."""
    from agent_backbone.services.database._queue_repo import dedup_key_for

    rows = sync_conn.execute(
        text("SELECT id, message, sender FROM message_queue WHERE dedup_key IS NULL")
    ).fetchall()
    for row in rows:
        sync_conn.execute(
            text("UPDATE message_queue SET dedup_key = :key WHERE id = :id"),
            {"key": dedup_key_for(row.message, row.sender or "", None), "id": row.id},
        )
    if rows:
        log.info("Backfilled dedup_key on %d queue rows (re-stamped database)", len(rows))


def _sqlite_version(sync_conn) -> tuple[int, ...] | None:
    """The SQLite library's version when the connection is SQLite, else None."""
    if sync_conn.dialect.name != "sqlite":
        return None
    import sqlite3

    return sqlite3.sqlite_version_info


def _repair_schema(sync_conn) -> None:
    """Bring an existing database up to the model on a re-stamp.

    Tables are stable pre-1.0, but columns and index *predicates* have
    changed (``flow_name`` became ``source``; the ``kind = 'issue'`` guard
    on the delivery owner index; the one queue dedup rule for every
    non-issue kind). A re-stamp is the only moment an existing database
    meets a regenerated squash, so columns are repaired and indexes are
    rebuilt from the model here. Duplicate pending queue rows — the reason
    the dedup index exists — are expired first so the unique index can be
    created.
    """
    _repair_columns(sync_conn)
    _backfill_dedup_keys(sync_conn)
    sync_conn.execute(
        text(
            """UPDATE message_queue SET status = 'expired'
               WHERE delivery_kind != 'issue' AND status IN ('pending','in_progress')
                 AND id NOT IN (
                   SELECT MIN(id) FROM message_queue
                   WHERE delivery_kind != 'issue' AND status IN ('pending','in_progress')
                   GROUP BY session_name, dedup_key
                 )"""
        )
    )
    for table in metadata.sorted_tables:
        for index in table.indexes:
            sync_conn.execute(text(f"DROP INDEX IF EXISTS {index.name}"))
            index.create(sync_conn)
    for name in _SUPERSEDED_INDEXES:
        sync_conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
    log.info("Rebuilt indexes from the model (re-stamped database)")


class BackboneDB:
    """The backbone's database: owns the engine, migrates on start, and holds
    one repository per table family (``db.deliveries.record(...)``,
    ``db.queue.enqueue(...)``, …).

    Usage (production — via LifecycleManager):
        db = BackboneDB(url)
        await db.start()
        ...
        await db.stop()

    Usage (tests):
        async with BackboneDB.connect() as db:
            await db.deliveries.record(...)
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._engine: AsyncEngine | None = None
        engine = lambda: self.engine  # noqa: E731 — the repos read the live engine
        self.deliveries = DeliveryRepo(engine)
        self.queue = QueueRepo(engine)
        self.acks = AcknowledgementRepo(engine)
        self.dependencies = DependencyRepo(engine)
        self.events = EventRepo(engine)
        self.settings = SettingRepo(engine)
        self.agents = AgentRepo(engine)
        self.swarms = SwarmRepo(engine)
        self.states = StateRepo(engine)

    @property
    def url(self) -> str:
        return self._url

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("BackboneDB is not started")
        return self._engine

    @property
    def _is_memory(self) -> bool:
        return ":memory:" in self._url

    # --- LifecycleAware methods ---

    async def start(self) -> None:
        """Connect, create the schema (SQLite) and run migrations (persistent databases)."""
        self._engine = build_engine(self._url)
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        log.info("Database connected: %s", redact_url(self._url))
        # PostgreSQL: Alembic owns the schema. SQLite: create_all is safe and
        # needed for fresh files / in-memory databases.
        if "postgresql" not in self._url:
            async with self._engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        if not self._is_memory:
            await self._run_migrations()

    async def stop(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def health_check(self) -> dict:
        """Check database connectivity."""
        return {
            "healthy": await self.check_connection(),
            "service": "database",
            "url": redact_url(self._url),
        }

    async def check_connection(self) -> bool:
        """Test database connectivity with a simple query."""
        if self._engine is None:
            return False
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    @classmethod
    @asynccontextmanager
    async def connect(cls, url: str = "sqlite+aiosqlite:///:memory:") -> AsyncIterator[BackboneDB]:
        """Context manager for test/ad-hoc usage — starts, yields, stops."""
        db = cls(url)
        await db.start()
        try:
            yield db
        finally:
            await db.stop()

    async def _run_migrations(self) -> None:
        """Run Alembic migrations for persistent databases."""
        from alembic import command
        from alembic.config import Config

        alembic_cfg = Config()
        alembic_cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))

        async with self._engine.begin() as async_conn:

            def _run_alembic(sync_conn):
                from alembic.script import ScriptDirectory
                from sqlalchemy import inspect

                inspector = inspect(sync_conn)
                existing_tables = set(inspector.get_table_names())
                has_alembic = "alembic_version" in existing_tables
                app_tables = {table.name for table in metadata.sorted_tables}
                existing_app_tables = existing_tables & app_tables

                alembic_cfg.attributes["connection"] = sync_conn
                if has_alembic and existing_app_tables == app_tables:
                    # Pre-1.0 policy: one squashed migration. A regenerated
                    # squash changes the revision id, so a database stamped
                    # with the old id must be re-stamped — its schema is
                    # already complete (SQLite: create_all above; the stamp
                    # is the entire migration history).
                    stored = sync_conn.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar()
                    script = ScriptDirectory.from_config(alembic_cfg)
                    known = {rev.revision for rev in script.walk_revisions()}
                    if stored not in known:
                        _repair_schema(sync_conn)
                        command.stamp(alembic_cfg, "head", purge=True)
                        return
                if has_alembic or not existing_app_tables:
                    command.upgrade(alembic_cfg, "head")
                elif existing_app_tables == app_tables:
                    # SQLite initializes schema with metadata.create_all() before
                    # this method runs, so stamp that full schema as current.
                    command.stamp(alembic_cfg, "head")
                else:
                    partial_tables = ", ".join(sorted(existing_app_tables))
                    raise RuntimeError(
                        "Database contains application tables but no alembic_version table; "
                        f"refusing to stamp partial schema: {partial_tables}"
                    )

            await async_conn.run_sync(_run_alembic)
