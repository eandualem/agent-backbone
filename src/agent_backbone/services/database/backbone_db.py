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


def _repair_schema(sync_conn) -> None:
    """Bring an existing database's indexes up to the model on a re-stamp.

    Tables are stable pre-1.0, but index *predicates* have changed (the
    ``kind = 'issue'`` guard on the delivery owner index, the one queue
    dedup rule for every non-issue kind). A re-stamp is the only moment an
    existing database meets a regenerated squash, so indexes are rebuilt
    from the model here. Duplicate pending queue rows — the reason the
    dedup index exists — are expired first so the unique index can be
    created.
    """
    sync_conn.execute(
        text(
            """UPDATE message_queue SET status = 'expired'
               WHERE delivery_kind != 'issue' AND status IN ('pending','in_progress')
                 AND id NOT IN (
                   SELECT MIN(id) FROM message_queue
                   WHERE delivery_kind != 'issue' AND status IN ('pending','in_progress')
                   GROUP BY session_name, content_hash
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
