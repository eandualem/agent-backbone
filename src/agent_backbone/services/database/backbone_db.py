"""Database persistence — AsyncEngine connection lifecycle and query delegation.

Uses SQLAlchemy AsyncEngine for connection pooling and dialect abstraction.
Production receives the engine from DatabaseService (single pool).
Tests use BackboneDB.connect() for standalone in-memory engines.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

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
_RepoResult = TypeVar("_RepoResult")


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

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[AsyncConnection]:
        """Open the standard transaction wrapper used by repository calls."""
        async with self._engine.begin() as conn:
            yield conn

    async def _run_repo(
        self,
        operation: Callable[..., Awaitable[_RepoResult]],
        /,
        *args: object,
        **kwargs: object,
    ) -> _RepoResult:
        """Run one repository operation inside the standard transaction scope."""
        async with self._connection() as conn:
            return await operation(conn, *args, **kwargs)

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
            async with self._connection() as conn:
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

        async with self._connection() as async_conn:

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
        return await self._run_repo(
            _delivery_repo.record_delivery,
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
        return await self._run_repo(
            _delivery_repo.query_deliveries,
            issue_number,
            target_entity,
            session_name,
            outcome,
            limit,
        )

    async def get_failed_deliveries(self, limit: int = 50) -> list[dict]:
        """Get deliveries with failed outcomes for retry."""
        return await self._run_repo(_delivery_repo.get_failed_deliveries, limit)

    async def prune_old_deliveries(self, retention_days: int = 30) -> int:
        """Delete delivery records older than retention period. Returns count deleted."""
        return await self._run_repo(_delivery_repo.prune_old_deliveries, retention_days)

    async def get_delivery_stats(self) -> list[dict]:
        """Get delivery counts grouped by outcome."""
        return await self._run_repo(_delivery_repo.get_delivery_stats)

    # --- Dedup (delegates to _queue_repo, except in-memory cache) ---

    async def is_duplicate_delivery_id(self, delivery_id: str) -> bool:
        """Check if a delivery ID has been seen before."""
        return await self._run_repo(_queue_repo.is_duplicate_delivery_id, delivery_id)

    async def record_delivery_id(self, delivery_id: str) -> None:
        """Record a delivery ID for dedup."""
        await self._run_repo(_queue_repo.record_delivery_id, delivery_id)

    async def prune_delivery_ids(self, max_age_hours: int = 24) -> int:
        """Remove old dedup entries. Returns count deleted."""
        return await self._run_repo(_queue_repo.prune_delivery_ids, max_age_hours)

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
            async with self._connection() as conn:
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
        return await self._run_repo(_state_repo.get_agent_state, session_name)

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
        await self._run_repo(
            _state_repo.set_agent_state,
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
        return await self._run_repo(_state_repo.get_all_agent_states)

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
        return await self._run_repo(
            _activity_repo.record_activity,
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
        return await self._run_repo(_activity_repo.record_activity_batch, rows)

    async def get_activity(
        self,
        session: str,
        limit: int = 50,
        since: str | None = None,
    ) -> list[dict]:
        """Query activity events for a session."""
        return await self._run_repo(_activity_repo.get_activity, session, limit, since)

    async def query_activity(
        self,
        *,
        limit: int = 50,
        events: list[str] | None = None,
    ) -> list[dict]:
        """Query activity events across all sessions."""
        return await self._run_repo(_activity_repo.query_activity, limit=limit, events=events)

    async def has_activity_event(
        self,
        *,
        session: str,
        event: str,
        source_ref: str | None = None,
        since: float | None = None,
    ) -> bool:
        """Whether a matching activity event exists for the session."""
        return await self._run_repo(
            _activity_repo.has_activity_event,
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
        return await self._run_repo(_telemetry_repo.get_checkpoint, session, source_ref)

    async def query_telemetry_checkpoints(
        self,
        *,
        session: str | None = None,
        runtime: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """List telemetry checkpoints with optional filters."""
        return await self._run_repo(
            _telemetry_repo.query_checkpoints,
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
        await self._run_repo(
            _telemetry_repo.upsert_checkpoint,
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
        await self._run_repo(_queue_repo.upsert_dependency, parent, sub)

    async def get_parents(self, sub_issue_number: int) -> list[int]:
        """Get parent issue numbers for a given sub-issue."""
        return await self._run_repo(_queue_repo.get_parents, sub_issue_number)

    async def sync_dependencies(self, parent: int, sub_issues: list[int]) -> None:
        """Sync the dependency table for a parent."""
        await self._run_repo(_queue_repo.sync_dependencies, parent, sub_issues)

    # --- Acknowledgments (delegates to _queue_repo) ---

    async def record_acknowledgment(self, issue_number: int, target_entity: str) -> None:
        """Record that an entity has acknowledged an issue."""
        await self._run_repo(_queue_repo.record_acknowledgment, issue_number, target_entity)

    async def is_acknowledged(self, issue_number: int, target_entity: str) -> bool:
        """Check if entity has acknowledged this issue."""
        return await self._run_repo(_queue_repo.is_acknowledged, issue_number, target_entity)

    async def clear_acknowledgment(self, issue_number: int, target_entity: str) -> None:
        """Clear acknowledgment for entity on issue."""
        await self._run_repo(_queue_repo.clear_acknowledgment, issue_number, target_entity)

    # --- Heartbeats (delegates to _queue_repo) ---

    async def record_heartbeat(self, agent: str, outcome: str, message: str | None = None) -> int:
        """Record a heartbeat delivery attempt. Returns the row ID."""
        return await self._run_repo(_queue_repo.record_heartbeat, agent, outcome, message)

    async def get_last_heartbeat(self, agent: str, outcome: str = "delivered") -> str | None:
        """Get the delivered_at ISO string of the most recent matching heartbeat."""
        return await self._run_repo(_queue_repo.get_last_heartbeat, agent, outcome)

    async def query_heartbeats(
        self,
        agent: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query heartbeat records with optional filters."""
        return await self._run_repo(_queue_repo.query_heartbeats, agent, outcome, limit)

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
        return await self._run_repo(
            _queue_repo.enqueue_message,
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
        return await self._run_repo(_queue_repo.dequeue_messages, session_name, limit)

    async def mark_message_delivered(self, message_id: int) -> None:
        """Mark a queued message as delivered."""
        await self._run_repo(_queue_repo.mark_message_delivered, message_id)

    async def get_message_by_id(self, message_id: int) -> dict | None:
        """Get a single message by ID (for verification)."""
        async with self._connection() as conn:
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
        return await self._run_repo(
            _swarm_repo.create_swarm,
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
        return await self._run_repo(_swarm_repo.list_swarms, repo=repo, status=status)

    async def get_swarm(self, swarm_id: str) -> dict | None:
        """Get a single swarm with its workers."""
        return await self._run_repo(_swarm_repo.get_swarm, swarm_id)

    async def update_swarm_worker_status(
        self,
        swarm_id: str,
        worker_name: str,
        status: str,
        pr_number: int | None = None,
    ) -> dict | None:
        """Update one worker status and return the refreshed swarm."""
        return await self._run_repo(
            _swarm_repo.update_worker_status,
            swarm_id,
            worker_name,
            status,
            pr_number=pr_number,
        )

    async def complete_swarm(self, swarm_id: str) -> dict | None:
        """Mark a swarm completed and return the refreshed swarm."""
        return await self._run_repo(_swarm_repo.complete_swarm, swarm_id)
