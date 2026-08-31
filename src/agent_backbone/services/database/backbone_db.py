"""Database persistence — AsyncEngine connection lifecycle and query delegation.

Uses SQLAlchemy AsyncEngine for connection pooling and dialect abstraction.
Production receives the engine from DatabaseService (single pool).
Tests use BackboneDB.connect() for standalone in-memory engines.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import agent_backbone.services.database.models  # noqa: F401  (registers the ORM tables)
from agent_backbone.services.database import (
    _agents_repo,
    _delivery_repo,
    _events_repo,
    _queue_repo,
    _settings_repo,
    _state_repo,
    _swarms_repo,
)
from agent_backbone.services.database.base import Base
from agent_backbone.services.database.interface import build_engine

metadata = Base.metadata

log = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
"""Migrations ship inside the package so installed CLIs migrate from anywhere."""


class BackboneDB:
    """Async database for backbone persistence.

    Usage (production — via LifecycleManager):
        db = BackboneDB(database_service=service)
        await db.start()
        ...
        await db.stop()

    Usage (tests):
        async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
            await db.record_delivery(...)
    """

    def __init__(self, engine: AsyncEngine | None = None, *, database_service=None) -> None:
        self._engine: AsyncEngine | None = engine
        self._database_service = database_service
        self._seen_deliveries: OrderedDict[str, bool] = OrderedDict()

    @property
    def _is_memory(self) -> bool:
        return self._engine is not None and ":memory:" in str(self._engine.url)

    # --- LifecycleAware methods ---

    async def start(self) -> None:
        """Create schema and run migrations. Engine is provided externally."""
        if self._engine is None and self._database_service is not None:
            self._engine = self._database_service.engine
        if self._engine is None:
            raise RuntimeError("BackboneDB requires an engine or database_service")
        # PostgreSQL: Alembic owns the schema. SQLite: create_all is safe and
        # needed for fresh files / in-memory databases.
        if "postgresql" not in str(self._engine.url):
            async with self._engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        if not self._is_memory:
            await self._run_migrations()

    async def stop(self) -> None:
        """Release engine reference. Engine lifecycle is owned by DatabaseService."""
        self._engine = None

    async def health_check(self) -> dict:
        """Check database connectivity."""
        healthy = await self.check_connection()
        return {
            "healthy": healthy,
            "service": "persistence",
            "connected": self._engine is not None,
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

    @asynccontextmanager
    @staticmethod
    async def connect(
        database_url: str = "sqlite+aiosqlite:///:memory:",
    ) -> AsyncIterator[BackboneDB]:
        """Context manager for test/ad-hoc usage — creates standalone engine, yields, disposes."""
        engine = build_engine(database_url)
        db = BackboneDB(engine)
        await db.start()
        try:
            yield db
        finally:
            db._engine = None
            await engine.dispose()

    async def _run_migrations(self) -> None:
        """Run Alembic migrations for persistent databases."""
        from alembic.config import Config

        from alembic import command

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

    # --- Delivery tracking (delegates to _delivery_repo) ---

    async def record_delivery(
        self,
        issue_number: int | None,
        target_entity: str,
        session_name: str,
        outcome: str,
        flow_name: str = "",
        flow_run_id: str = "",
        *,
        repo: str = "",
        kind: str = "issue",
        preview: str = "",
    ) -> int:
        """Record a delivery attempt. Returns the row ID."""
        async with self._engine.begin() as conn:
            return await _delivery_repo.record_delivery(
                conn,
                issue_number,
                target_entity,
                session_name,
                outcome,
                flow_name,
                flow_run_id,
                repo=repo,
                kind=kind,
                preview=preview,
            )

    async def claim_delivery_attempt(
        self,
        issue_number: int,
        target_entity: str,
        session_name: str,
        flow_name: str,
        *,
        repo: str = "",
        preview: str = "",
    ) -> int | None:
        """Reserve an issue delivery attempt before sending."""
        async with self._engine.begin() as conn:
            return await _delivery_repo.claim_delivery_attempt(
                conn,
                issue_number,
                target_entity,
                session_name,
                flow_name,
                repo=repo,
                preview=preview,
            )

    async def finalize_delivery_attempt(self, delivery_id: int, outcome: str) -> None:
        """Finalize a previously claimed delivery attempt."""
        async with self._engine.begin() as conn:
            await _delivery_repo.finalize_delivery_attempt(conn, delivery_id, outcome)

    async def reclaim_stale_attempts(self, max_age_minutes: int = 5) -> int:
        """Delete stale attempting rows so new claims can proceed."""
        async with self._engine.begin() as conn:
            return await _delivery_repo.reclaim_stale_attempts(conn, max_age_minutes)

    async def query_deliveries(
        self,
        issue_number: int | None = None,
        target_entity: str | None = None,
        session_name: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
        *,
        repo: str | None = None,
        kind: str | None = None,
    ) -> list[dict]:
        """Query delivery records with optional filters."""
        async with self._engine.begin() as conn:
            return await _delivery_repo.query_deliveries(
                conn,
                issue_number,
                target_entity,
                session_name,
                outcome,
                limit,
                repo=repo,
                kind=kind,
            )

    async def get_failed_deliveries(self, limit: int = 50) -> list[dict]:
        """Get deliveries with failed outcomes for retry."""
        async with self._engine.begin() as conn:
            return await _delivery_repo.get_failed_deliveries(conn, limit)

    async def prune_old_deliveries(self, retention_days: int = 30) -> int:
        """Delete delivery records older than retention period. Returns count deleted."""
        async with self._engine.begin() as conn:
            return await _delivery_repo.prune_old_deliveries(conn, retention_days)

    async def get_delivery_stats(self) -> list[dict]:
        """Get delivery counts grouped by outcome."""
        async with self._engine.begin() as conn:
            return await _delivery_repo.get_delivery_stats(conn)

    # --- Webhook dedup hot cache (the events table is the durable record) ---

    def is_duplicate(self, delivery_id: str, max_ids: int = 100) -> bool:
        """Check and record delivery ID in hot cache for dedup."""
        if not delivery_id:
            return False
        if delivery_id in self._seen_deliveries:
            return True
        self._seen_deliveries[delivery_id] = True
        while len(self._seen_deliveries) > max_ids:
            self._seen_deliveries.popitem(last=False)
        return False

    # --- Agent state (delegates to _state_repo) ---

    async def get_agent_state(self, session_name: str) -> dict | None:
        """Get the current state record for an agent session."""
        async with self._engine.begin() as conn:
            return await _state_repo.get_agent_state(conn, session_name)

    async def set_agent_state(
        self,
        session_name: str,
        state: str,
        current_issue: int | None = None,
        started_at: str | None = None,
        ts: str | None = None,
        plan_file: str | None = None,
        plan_title: str | None = None,
        reason: str | None = None,
        current_repo: str | None = None,
    ) -> None:
        """Upsert agent state."""
        async with self._engine.begin() as conn:
            await _state_repo.set_agent_state(
                conn,
                session_name,
                state,
                current_issue,
                started_at,
                ts=ts,
                plan_file=plan_file,
                plan_title=plan_title,
                reason=reason,
                current_repo=current_repo,
            )

    async def get_all_agent_states(self) -> list[dict]:
        """Get state records for all tracked agents."""
        async with self._engine.begin() as conn:
            return await _state_repo.get_all_agent_states(conn)

    # --- Swarms (delegates to _swarms_repo) ---

    async def create_swarm(self, name: str, **fields) -> None:
        """Record a new active swarm."""
        async with self._engine.begin() as conn:
            await _swarms_repo.create_swarm(conn, name, **fields)

    async def get_swarm(self, name: str) -> dict | None:
        async with self._engine.begin() as conn:
            return await _swarms_repo.get_swarm(conn, name)

    async def list_swarms(self, *, active_only: bool = False) -> list[dict]:
        async with self._engine.begin() as conn:
            return await _swarms_repo.list_swarms(conn, active_only=active_only)

    async def find_active_swarm_for_issue(self, repo: str, issue_number: int) -> dict | None:
        """The active swarm working a given issue, if any."""
        async with self._engine.begin() as conn:
            return await _swarms_repo.find_active_swarm_for_issue(conn, repo, issue_number)

    async def set_swarm_status(self, name: str, status: str) -> None:
        async with self._engine.begin() as conn:
            await _swarms_repo.set_swarm_status(conn, name, status)

    # --- Issue dependencies (delegates to _queue_repo) ---

    async def get_parents(self, sub_issue_number: int, *, repo: str = "") -> list[int]:
        """Get parent issue numbers for a given sub-issue."""
        async with self._engine.begin() as conn:
            return await _queue_repo.get_parents(conn, sub_issue_number, repo=repo)

    async def sync_dependencies(
        self, parent: int, sub_issues: list[int], *, repo: str = ""
    ) -> None:
        """Sync the dependency table for a parent."""
        async with self._engine.begin() as conn:
            await _queue_repo.sync_dependencies(conn, parent, sub_issues, repo=repo)

    # --- Acknowledgments (delegates to _queue_repo) ---

    async def record_acknowledgment(
        self, issue_number: int, target_entity: str, *, repo: str = ""
    ) -> None:
        """Record that an entity has acknowledged an issue."""
        async with self._engine.begin() as conn:
            await _queue_repo.record_acknowledgment(conn, issue_number, target_entity, repo=repo)

    async def is_acknowledged(
        self, issue_number: int, target_entity: str, *, repo: str = ""
    ) -> bool:
        """Check if entity has acknowledged this issue."""
        async with self._engine.begin() as conn:
            return await _queue_repo.is_acknowledged(conn, issue_number, target_entity, repo=repo)

    async def clear_acknowledgment(
        self, issue_number: int, target_entity: str, *, repo: str = ""
    ) -> None:
        """Clear acknowledgment for entity on issue."""
        async with self._engine.begin() as conn:
            await _queue_repo.clear_acknowledgment(conn, issue_number, target_entity, repo=repo)

    # --- Message queue (delegates to _queue_repo) ---

    async def enqueue_message(
        self,
        session_name: str,
        message: str,
        issue_number: int | None = None,
        target_entity: str | None = None,
        delivery_kind: str = "issue",
        flow_name: str = "",
        *,
        repo: str = "",
    ) -> int:
        """Enqueue a message for later delivery. Returns the row ID."""
        async with self._engine.begin() as conn:
            return await _queue_repo.enqueue_message(
                conn,
                session_name,
                message,
                issue_number,
                target_entity,
                delivery_kind,
                flow_name,
                repo=repo,
            )

    async def dequeue_messages(
        self,
        session_name: str,
        limit: int = 10,
    ) -> list[dict]:
        """Atomically claim pending messages for a session, oldest first."""
        async with self._engine.begin() as conn:
            return await _queue_repo.dequeue_messages(conn, session_name, limit)

    async def get_sessions_with_pending(self) -> list[str]:
        """List sessions that currently have pending queue rows."""
        async with self._engine.begin() as conn:
            return await _queue_repo.get_sessions_with_pending(conn)

    async def release_lease(self, message_id: int) -> None:
        """Return a claimed queued message to pending."""
        async with self._engine.begin() as conn:
            await _queue_repo.release_lease(conn, message_id)

    async def expire_stale_leases(self, max_age_minutes: int = 5) -> int:
        """Recover stale queue leases older than max_age_minutes."""
        async with self._engine.begin() as conn:
            return await _queue_repo.expire_stale_leases(conn, max_age_minutes)

    async def mark_message_delivered(self, message_id: int) -> None:
        """Mark a queued message as delivered."""
        async with self._engine.begin() as conn:
            await _queue_repo.mark_message_delivered(conn, message_id)

    async def purge_pending_for_issue(self, issue_number: int, *, repo: str = "") -> int:
        """Mark all pending queue messages for an issue as delivered."""
        async with self._engine.begin() as conn:
            return await _queue_repo.purge_pending_for_issue(conn, issue_number, repo=repo)

    async def expire_stale_pending(self, max_age_minutes: int = 30) -> int:
        """Expire pending queue messages older than max_age_minutes."""
        async with self._engine.begin() as conn:
            return await _queue_repo.expire_stale_pending(conn, max_age_minutes)

    async def get_message_by_id(self, message_id: int) -> dict | None:
        """Get a single message by ID (for verification)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("SELECT * FROM message_queue WHERE id = :id"),
                {"id": message_id},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None

    # --- Settings (delegates to _settings_repo) ---

    async def get_all_settings(self) -> dict[str, object]:
        async with self._engine.begin() as conn:
            return await _settings_repo.get_all_settings(conn)

    async def set_setting(self, key: str, value: object) -> None:
        async with self._engine.begin() as conn:
            await _settings_repo.set_setting(conn, key, value)

    async def delete_setting(self, key: str) -> bool:
        async with self._engine.begin() as conn:
            return await _settings_repo.delete_setting(conn, key)

    # --- Agents (delegates to _agents_repo) ---

    async def list_agents(self) -> list[dict]:
        async with self._engine.begin() as conn:
            return await _agents_repo.list_agents(conn)

    async def upsert_agent(self, name: str, **fields) -> None:
        async with self._engine.begin() as conn:
            await _agents_repo.upsert_agent(conn, name, **fields)

    async def touch_agent_started(self, name: str) -> None:
        async with self._engine.begin() as conn:
            await _agents_repo.touch_agent_started(conn, name)

    async def delete_agent(self, name: str) -> bool:
        async with self._engine.begin() as conn:
            return await _agents_repo.delete_agent(conn, name)

    async def add_watch(self, name: str, repo: str) -> None:
        async with self._engine.begin() as conn:
            await _agents_repo.add_watch(conn, name, repo)

    async def remove_watch(self, name: str, repo: str) -> bool:
        async with self._engine.begin() as conn:
            return await _agents_repo.remove_watch(conn, name, repo)

    # --- Events (delegates to _events_repo) ---

    async def record_event(self, **fields) -> int | None:
        async with self._engine.begin() as conn:
            return await _events_repo.record_event(conn, **fields)

    async def mark_event_processed(self, event_id: int, outcome: str) -> None:
        async with self._engine.begin() as conn:
            await _events_repo.mark_event_processed(conn, event_id, outcome)

    async def query_events(self, *, repo: str | None = None, limit: int = 50) -> list[dict]:
        async with self._engine.begin() as conn:
            return await _events_repo.query_events(conn, repo=repo, limit=limit)

    async def last_event_time_by_repo(self) -> dict[str, str]:
        async with self._engine.begin() as conn:
            return await _events_repo.last_event_time_by_repo(conn)

    async def prune_events(self, retention_days: int = 30) -> int:
        async with self._engine.begin() as conn:
            return await _events_repo.prune_events(conn, retention_days)
