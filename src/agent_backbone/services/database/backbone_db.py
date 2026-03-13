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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agent_backbone.services.database import (
    _activity_repo,
    _delivery_repo,
    _queue_repo,
    _state_repo,
    _swarm_repo,
    _telemetry_repo,
)
from agent_backbone.services.database.base import Base
from agent_backbone.services.database.models import (  # noqa: F401
    AcknowledgmentORM,
    AgentActivityORM,
    AgentStateORM,
    DedupLogORM,
    DeliveryORM,
    HeartbeatORM,
    IssueDependencyORM,
    MessageQueueORM,
    SwarmORM,
    SwarmWorkerORM,
    TelemetryCheckpointORM,
)

metadata = Base.metadata

log = logging.getLogger(__name__)

# Path to alembic.ini relative to this file:
# src/agent_backbone/services/database/backbone_db.py → repo root
_ALEMBIC_INI = Path(__file__).parents[4] / "alembic.ini"


class BackboneDB:
    """Async database for backbone persistence.

    Usage (production — via LifecycleManager):
        db = BackboneDB(engine)          # engine from DatabaseService
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
        # Resolve engine from DatabaseService if not provided directly (lazy — service starts first)
        if self._engine is None and self._database_service is not None:
            self._engine = self._database_service.engine
        if self._engine is None:
            raise RuntimeError("BackboneDB requires an engine or database_service")
        # PostgreSQL (asyncpg): skip create_all — checkfirst unreliable, Alembic owns schema
        # SQLite (memory or file): create_all is safe and needed for schema init
        if "postgresql" not in str(self._engine.url):
            async with self._engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        if not self._is_memory:
            await self._run_migrations()
        await self.load_dedup_cache()

    async def stop(self) -> None:
        """Release engine reference. Engine lifecycle is owned by DatabaseService."""
        self._engine = None

    async def health_check(self) -> dict:
        """Check database connectivity."""
        healthy = await self.check_connection()
        return {
            "healthy": healthy,
            "service": "persistence",
            "database_url": str(self._engine.url) if self._engine else "",
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
        engine = create_async_engine(database_url)
        db = BackboneDB(engine)
        await db.start()
        try:
            yield db
        finally:
            db._engine = None
            await engine.dispose()

    async def _run_migrations(self) -> None:
        """Run Alembic migrations for persistent databases.

        Skipped for :memory: databases (they use metadata.create_all() instead).
        """
        from alembic.config import Config

        from alembic import command

        alembic_cfg = Config(str(_ALEMBIC_INI))

        async with self._engine.begin() as async_conn:

            def _run_alembic(sync_conn):
                from sqlalchemy import inspect

                inspector = inspect(sync_conn)
                existing_tables = set(inspector.get_table_names())
                has_alembic = "alembic_version" in existing_tables
                app_tables = {table.name for table in metadata.sorted_tables}
                existing_app_tables = existing_tables & app_tables

                alembic_cfg.attributes["connection"] = sync_conn
                if has_alembic:
                    command.upgrade(alembic_cfg, "head")
                elif not existing_app_tables:
                    command.upgrade(alembic_cfg, "head")
                elif existing_app_tables == app_tables:
                    # SQLite file tests initialize schema with metadata.create_all()
                    # before this method runs, so stamp that full schema as current.
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
        issue_number: int,
        target_entity: str,
        session_name: str,
        outcome: str,
        flow_name: str = "",
        flow_run_id: str = "",
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
            )

    async def query_deliveries(
        self,
        issue_number: int | None = None,
        target_entity: str | None = None,
        session_name: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
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

    # --- Dedup (delegates to _queue_repo, except in-memory cache) ---

    async def is_duplicate_delivery_id(self, delivery_id: str) -> bool:
        """Check if a delivery ID has been seen before."""
        async with self._engine.begin() as conn:
            return await _queue_repo.is_duplicate_delivery_id(conn, delivery_id)

    async def record_delivery_id(self, delivery_id: str) -> None:
        """Record a delivery ID for dedup."""
        async with self._engine.begin() as conn:
            await _queue_repo.record_delivery_id(conn, delivery_id)

    async def prune_delivery_ids(self, max_age_hours: int = 24) -> int:
        """Remove old dedup entries. Returns count deleted."""
        async with self._engine.begin() as conn:
            return await _queue_repo.prune_delivery_ids(conn, max_age_hours)

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

    async def load_dedup_cache(self, max_ids: int = 100) -> None:
        """Load recent delivery IDs from database into hot cache on startup."""
        try:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    text("SELECT delivery_id FROM dedup_log ORDER BY received_at DESC LIMIT :lim"),
                    {"lim": max_ids},
                )
                rows = result.fetchall()
                for row in reversed(rows):
                    self._seen_deliveries[row._mapping["delivery_id"]] = True
                log.info("Loaded %d delivery IDs into hot cache", len(self._seen_deliveries))
        except Exception:
            log.warning("Could not load dedup state from database — starting fresh")

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
        last_activity: str | None = None,
        started_at: str | None = None,
        entity: str | None = None,
        context: str | None = None,
        ts: str | None = None,
        plan_file: str | None = None,
        plan_title: str | None = None,
    ) -> None:
        """Upsert agent state."""
        async with self._engine.begin() as conn:
            await _state_repo.set_agent_state(
                conn,
                session_name,
                state,
                current_issue,
                last_activity,
                started_at,
                entity=entity,
                context=context,
                ts=ts,
                plan_file=plan_file,
                plan_title=plan_title,
            )

    async def get_all_agent_states(self) -> list[dict]:
        """Get state records for all tracked agents."""
        async with self._engine.begin() as conn:
            return await _state_repo.get_all_agent_states(conn)

    # --- Agent activity (delegates to _activity_repo) ---

    async def record_activity(
        self,
        session: str,
        event: str,
        data: str | None,
        ts: str,
        *,
        entity: str | None = None,
        runtime: str | None = None,
        source_kind: str | None = None,
        source_ref: str | None = None,
        source_event_id: str | None = None,
        trace_id: str | None = None,
        parent_trace_id: str | None = None,
        model: str | None = None,
    ) -> int:
        """Record an agent activity event. Returns the row ID."""
        async with self._engine.begin() as conn:
            return await _activity_repo.record_activity(
                conn,
                session,
                event,
                data,
                ts,
                entity=entity,
                runtime=runtime,
                source_kind=source_kind,
                source_ref=source_ref,
                source_event_id=source_event_id,
                trace_id=trace_id,
                parent_trace_id=parent_trace_id,
                model=model,
            )

    async def record_activity_batch(
        self,
        rows: list[dict[str, str | None]],
    ) -> int:
        """Record a batch of activity events. Returns the number of inserted rows."""
        async with self._engine.begin() as conn:
            return await _activity_repo.record_activity_batch(conn, rows)

    async def get_activity(
        self,
        session: str,
        limit: int = 50,
        since: str | None = None,
    ) -> list[dict]:
        """Query activity events for a session."""
        async with self._engine.begin() as conn:
            return await _activity_repo.get_activity(conn, session, limit, since)

    async def query_activity(
        self,
        *,
        limit: int = 50,
        events: list[str] | None = None,
    ) -> list[dict]:
        """Query activity events across all sessions."""
        async with self._engine.begin() as conn:
            return await _activity_repo.query_activity(conn, limit=limit, events=events)

    async def has_activity_event(
        self,
        *,
        session: str,
        event: str,
        source_ref: str | None = None,
        since: float | None = None,
    ) -> bool:
        """Whether a matching activity event exists for the session."""
        async with self._engine.begin() as conn:
            return await _activity_repo.has_activity_event(
                conn,
                session=session,
                event=event,
                source_ref=source_ref,
                since=since,
            )

    async def get_telemetry_checkpoint(
        self,
        session: str,
        source_ref: str,
    ) -> dict | None:
        """Fetch the checkpoint for one telemetry source."""
        async with self._engine.begin() as conn:
            return await _telemetry_repo.get_checkpoint(conn, session, source_ref)

    async def query_telemetry_checkpoints(
        self,
        *,
        session: str | None = None,
        runtime: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """List telemetry checkpoints with optional filters."""
        async with self._engine.begin() as conn:
            return await _telemetry_repo.query_checkpoints(
                conn,
                session=session,
                runtime=runtime,
                limit=limit,
            )

    async def upsert_telemetry_checkpoint(
        self,
        *,
        session: str,
        source_ref: str,
        runtime: str,
        source_kind: str,
        checkpoint: dict[str, object] | None,
        entity: str | None = None,
        last_event_ts: str | None = None,
    ) -> None:
        """Create or update a telemetry checkpoint."""
        async with self._engine.begin() as conn:
            await _telemetry_repo.upsert_checkpoint(
                conn,
                session=session,
                source_ref=source_ref,
                runtime=runtime,
                source_kind=source_kind,
                checkpoint=checkpoint,
                entity=entity,
                last_event_ts=last_event_ts,
            )

    # --- Issue dependencies (delegates to _queue_repo) ---

    async def upsert_dependency(self, parent: int, sub: int) -> None:
        """Record a parent->sub-issue dependency."""
        async with self._engine.begin() as conn:
            await _queue_repo.upsert_dependency(conn, parent, sub)

    async def get_parents(self, sub_issue_number: int) -> list[int]:
        """Get parent issue numbers for a given sub-issue."""
        async with self._engine.begin() as conn:
            return await _queue_repo.get_parents(conn, sub_issue_number)

    async def sync_dependencies(self, parent: int, sub_issues: list[int]) -> None:
        """Sync the dependency table for a parent."""
        async with self._engine.begin() as conn:
            await _queue_repo.sync_dependencies(conn, parent, sub_issues)

    # --- Acknowledgments (delegates to _queue_repo) ---

    async def record_acknowledgment(self, issue_number: int, target_entity: str) -> None:
        """Record that an entity has acknowledged an issue."""
        async with self._engine.begin() as conn:
            await _queue_repo.record_acknowledgment(conn, issue_number, target_entity)

    async def is_acknowledged(self, issue_number: int, target_entity: str) -> bool:
        """Check if entity has acknowledged this issue."""
        async with self._engine.begin() as conn:
            return await _queue_repo.is_acknowledged(conn, issue_number, target_entity)

    async def clear_acknowledgment(self, issue_number: int, target_entity: str) -> None:
        """Clear acknowledgment for entity on issue."""
        async with self._engine.begin() as conn:
            await _queue_repo.clear_acknowledgment(conn, issue_number, target_entity)

    # --- Heartbeats (delegates to _queue_repo) ---

    async def record_heartbeat(self, agent: str, outcome: str, message: str | None = None) -> int:
        """Record a heartbeat delivery attempt. Returns the row ID."""
        async with self._engine.begin() as conn:
            return await _queue_repo.record_heartbeat(conn, agent, outcome, message)

    async def get_last_heartbeat(self, agent: str, outcome: str = "delivered") -> str | None:
        """Get the delivered_at ISO string of the most recent matching heartbeat."""
        async with self._engine.begin() as conn:
            return await _queue_repo.get_last_heartbeat(conn, agent, outcome)

    async def query_heartbeats(
        self,
        agent: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query heartbeat records with optional filters."""
        async with self._engine.begin() as conn:
            return await _queue_repo.query_heartbeats(conn, agent, outcome, limit)

    # --- Message queue (delegates to _queue_repo) ---

    async def enqueue_message(
        self,
        session_name: str,
        message: str,
        issue_number: int | None = None,
        target_entity: str | None = None,
        flow_name: str = "",
    ) -> int:
        """Enqueue a message for later delivery. Returns the row ID."""
        async with self._engine.begin() as conn:
            return await _queue_repo.enqueue_message(
                conn,
                session_name,
                message,
                issue_number,
                target_entity,
                flow_name,
            )

    async def dequeue_messages(
        self,
        session_name: str,
        limit: int = 10,
    ) -> list[dict]:
        """Get pending messages for a session, oldest first."""
        async with self._engine.begin() as conn:
            return await _queue_repo.dequeue_messages(conn, session_name, limit)

    async def mark_message_delivered(self, message_id: int) -> None:
        """Mark a queued message as delivered."""
        async with self._engine.begin() as conn:
            await _queue_repo.mark_message_delivered(conn, message_id)

    async def get_message_by_id(self, message_id: int) -> dict | None:
        """Get a single message by ID (for verification)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("SELECT * FROM message_queue WHERE id = :id"),
                {"id": message_id},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None

    # --- Swarm registry (delegates to _swarm_repo) ---

    async def create_swarm(
        self,
        repo: str,
        task_id: str | None,
        coding_agent_session: str,
        workers: list[dict],
    ) -> str:
        """Create a swarm and its workers. Returns the swarm ID."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.create_swarm(
                conn,
                repo,
                task_id,
                coding_agent_session,
                workers,
            )

    async def list_swarms(
        self,
        repo: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """List swarms with worker progress."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.list_swarms(conn, repo=repo, status=status)

    async def get_swarm(self, swarm_id: str) -> dict | None:
        """Get a single swarm with its workers."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.get_swarm(conn, swarm_id)

    async def update_swarm_worker_status(
        self,
        swarm_id: str,
        worker_name: str,
        status: str,
        pr_number: int | None = None,
    ) -> dict | None:
        """Update one worker status and return the refreshed swarm."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.update_worker_status(
                conn,
                swarm_id,
                worker_name,
                status,
                pr_number=pr_number,
            )

    async def complete_swarm(self, swarm_id: str) -> dict | None:
        """Mark a swarm completed and return the refreshed swarm."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.complete_swarm(conn, swarm_id)
