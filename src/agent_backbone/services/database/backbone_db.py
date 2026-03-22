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
    _analytics_repo,
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
    GovernanceTrackInstanceORM,
    GovernanceTrackORM,
    HeartbeatORM,
    IssueDependencyORM,
    MessageQueueORM,
    SwarmMessageORM,
    SwarmORM,
    SwarmPhaseHistoryORM,
    SwarmWorkerORM,
    TelemetryCheckpointORM,
)

metadata = Base.metadata

log = logging.getLogger(__name__)

# Path to alembic.ini relative to this file:
# src/agent_backbone/services/database/backbone_db.py → repo root
_ALEMBIC_INI = Path(__file__).parents[4] / "alembic.ini"


def _coerce_repo_issue_target(
    repo_full_name: str | int | None,
    issue_number: int | str,
    target_entity: str | None,
) -> tuple[str | None, int, str]:
    """Accept both repo-aware and legacy `(issue_number, target_entity)` calls."""
    if target_entity is None:
        if not isinstance(repo_full_name, int) or not isinstance(issue_number, str):
            raise TypeError("expected (issue_number: int, target_entity: str)")
        return None, repo_full_name, issue_number

    if not isinstance(issue_number, int) or not isinstance(target_entity, str):
        raise TypeError("expected (repo_full_name, issue_number: int, target_entity: str)")
    if repo_full_name is not None and not isinstance(repo_full_name, str):
        raise TypeError("repo_full_name must be a string or None")
    return repo_full_name, issue_number, target_entity


def _coerce_repo_issue(
    repo_full_name: str | int | None,
    issue_number: int | None,
) -> tuple[str | None, int]:
    """Accept both repo-aware and legacy `(issue_number,)` calls."""
    if issue_number is None:
        if not isinstance(repo_full_name, int):
            raise TypeError("expected issue_number: int")
        return None, repo_full_name

    if not isinstance(issue_number, int):
        raise TypeError("expected issue_number: int")
    if repo_full_name is not None and not isinstance(repo_full_name, str):
        raise TypeError("repo_full_name must be a string or None")
    return repo_full_name, issue_number


def _coerce_delivery_call(
    repo_full_name: str | int | None,
    issue_number: int | str,
    target_entity: str | None,
    session_name: str | None,
    outcome: str | None,
    flow_name: str,
    flow_run_id: str,
) -> tuple[str | None, int, str, str, str, str, str]:
    """Accept both repo-aware and legacy `record_delivery` call shapes."""
    if (
        isinstance(repo_full_name, int)
        and isinstance(issue_number, str)
        and isinstance(target_entity, str)
        and isinstance(session_name, str)
    ):
        return (
            None,
            repo_full_name,
            issue_number,
            target_entity,
            session_name,
            outcome or "",
            flow_name,
        )

    if not (
        isinstance(issue_number, int)
        and isinstance(target_entity, str)
        and isinstance(session_name, str)
        and isinstance(outcome, str)
    ):
        raise TypeError(
            "expected (repo_full_name, issue_number: int, target_entity: str, "
            "session_name: str, outcome: str)"
        )
    if repo_full_name is not None and not isinstance(repo_full_name, str):
        raise TypeError("repo_full_name must be a string or None")
    return repo_full_name, issue_number, target_entity, session_name, outcome, flow_name, flow_run_id


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
        repo_full_name: str | int | None = None,
        issue_number: int | str = 0,
        target_entity: str | None = None,
        session_name: str | None = None,
        outcome: str | None = None,
        flow_name: str = "",
        flow_run_id: str = "",
    ) -> int:
        """Record a delivery attempt. Returns the row ID."""
        (
            repo_full_name,
            issue_number,
            target_entity,
            session_name,
            outcome,
            flow_name,
            flow_run_id,
        ) = (
            _coerce_delivery_call(
                repo_full_name,
                issue_number,
                target_entity,
                session_name,
                outcome,
                flow_name,
                flow_run_id,
            )
        )
        async with self._engine.begin() as conn:
            return await _delivery_repo.record_delivery(
                conn,
                repo_full_name,
                issue_number,
                target_entity,
                session_name,
                outcome,
                flow_name,
                flow_run_id,
            )

    async def claim_delivery_attempt(
        self,
        repo_full_name: str | None = None,
        issue_number: int = 0,
        target_entity: str = "",
        session_name: str = "",
        flow_name: str = "",
    ) -> int | None:
        """Reserve an issue delivery attempt before sending."""
        if issue_number <= 0 or not target_entity or not session_name:
            raise TypeError(
                "claim_delivery_attempt requires issue_number, target_entity, and session_name"
            )
        async with self._engine.begin() as conn:
            return await _delivery_repo.claim_delivery_attempt(
                conn,
                repo_full_name,
                issue_number,
                target_entity,
                session_name,
                flow_name,
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
        repo_full_name: str | None = None,
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
                repo_full_name,
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

    # --- Analytics queries (delegates to _analytics_repo) ---

    async def query_analytics_rows(
        self,
        *,
        sessions: list[str],
        since: float | None = None,
        until: float | None = None,
        events: list[str] | None = None,
        runtime: str | None = None,
        model: str | None = None,
        source_kind: str | None = None,
        source_ref: str | None = None,
        trace_id: str | None = None,
        parent_trace_id: str | None = None,
        limit: int | None = None,
        cursor_ts: str | None = None,
        cursor_id: int | None = None,
    ) -> list[dict]:
        """Query activity rows for analytics aggregation."""
        async with self._engine.begin() as conn:
            return await _analytics_repo.query_analytics_rows(
                conn,
                sessions=sessions,
                since=since,
                until=until,
                events=events,
                runtime=runtime,
                model=model,
                source_kind=source_kind,
                source_ref=source_ref,
                trace_id=trace_id,
                parent_trace_id=parent_trace_id,
                limit=limit,
                cursor_ts=cursor_ts,
                cursor_id=cursor_id,
            )

    async def get_swarm_sessions_for_agent(
        self,
        coding_agent_session: str,
        *,
        swarm_id: str | None = None,
        worker_name: str | None = None,
        worker_role: str | None = None,
    ) -> list[dict]:
        """Find swarm worker sessions for a coding agent."""
        async with self._engine.begin() as conn:
            return await _analytics_repo.get_swarm_sessions_for_agent(
                conn,
                coding_agent_session,
                swarm_id=swarm_id,
                worker_name=worker_name,
                worker_role=worker_role,
            )

    async def get_worker_swarm_attribution(
        self,
        worker_session: str,
    ) -> dict | None:
        """Look up swarm context for a session that is itself a worker."""
        async with self._engine.begin() as conn:
            return await _analytics_repo.get_worker_swarm_attribution(
                conn,
                worker_session,
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

    async def record_acknowledgment(
        self,
        repo_full_name: str | int | None,
        issue_number: int | str,
        target_entity: str | None = None,
    ) -> None:
        """Record that an entity has acknowledged an issue."""
        repo_full_name, issue_number, target_entity = _coerce_repo_issue_target(
            repo_full_name,
            issue_number,
            target_entity,
        )
        async with self._engine.begin() as conn:
            await _queue_repo.record_acknowledgment(
                conn,
                repo_full_name,
                issue_number,
                target_entity,
            )

    async def is_acknowledged(
        self,
        repo_full_name: str | int | None,
        issue_number: int | str,
        target_entity: str | None = None,
    ) -> bool:
        """Check if entity has acknowledged this issue."""
        repo_full_name, issue_number, target_entity = _coerce_repo_issue_target(
            repo_full_name,
            issue_number,
            target_entity,
        )
        async with self._engine.begin() as conn:
            return await _queue_repo.is_acknowledged(
                conn,
                repo_full_name,
                issue_number,
                target_entity,
            )

    async def clear_acknowledgment(
        self,
        repo_full_name: str | int | None,
        issue_number: int | str,
        target_entity: str | None = None,
    ) -> None:
        """Clear acknowledgment for entity on issue."""
        repo_full_name, issue_number, target_entity = _coerce_repo_issue_target(
            repo_full_name,
            issue_number,
            target_entity,
        )
        async with self._engine.begin() as conn:
            await _queue_repo.clear_acknowledgment(
                conn,
                repo_full_name,
                issue_number,
                target_entity,
            )

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
        repo_full_name: str | None = None,
        issue_number: int | None = None,
        target_entity: str | None = None,
        delivery_kind: str = "issue",
        flow_name: str = "",
    ) -> int:
        """Enqueue a message for later delivery. Returns the row ID."""
        async with self._engine.begin() as conn:
            return await _queue_repo.enqueue_message(
                conn,
                session_name,
                message,
                repo_full_name,
                issue_number,
                target_entity,
                delivery_kind,
                flow_name,
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

    async def mark_matching_messages_delivered(
        self,
        *,
        session_name: str,
        message: str,
        delivery_kind: str,
        repo_full_name: str | None = None,
        issue_number: int | None = None,
    ) -> int:
        """Mark queued rows with the same delivery identity as delivered."""
        async with self._engine.begin() as conn:
            return await _queue_repo.mark_matching_messages_delivered(
                conn,
                session_name=session_name,
                message=message,
                delivery_kind=delivery_kind,
                repo_full_name=repo_full_name,
                issue_number=issue_number,
            )

    async def purge_pending_for_issue(
        self,
        repo_full_name: str | int | None,
        issue_number: int | None = None,
    ) -> int:
        """Mark all pending queue messages for an issue as delivered."""
        repo_full_name, issue_number = _coerce_repo_issue(repo_full_name, issue_number)
        async with self._engine.begin() as conn:
            return await _queue_repo.purge_pending_for_issue(conn, repo_full_name, issue_number)

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
        phase: str | None = None,
    ) -> list[dict]:
        """List swarms with worker progress."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.list_swarms(conn, repo=repo, phase=phase)

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

    async def complete_swarm_worker(
        self,
        swarm_id: str,
        worker_name: str,
        status: str,
        summary: str,
        pr_number: int | None = None,
    ) -> dict | None:
        """Mark a worker done/failed and return the refreshed swarm."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.complete_worker(
                conn,
                swarm_id,
                worker_name,
                status,
                summary,
                pr_number=pr_number,
            )

    async def update_swarm_phase(self, swarm_id: str, phase: str) -> dict | None:
        """Update swarm phase and return the refreshed swarm."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.update_swarm_phase(conn, swarm_id, phase)

    async def complete_swarm(self, swarm_id: str) -> dict | None:
        """Mark a swarm cleaned up and return the refreshed swarm."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.complete_swarm(conn, swarm_id)

    async def create_swarm_assignment(
        self,
        swarm_id: str,
        worker_name: str,
        *,
        assigned_by: str,
        summary: str,
        file_paths: list[str],
    ) -> dict | None:
        """Create one worker assignment in a collaborative swarm."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.create_assignment(
                conn,
                swarm_id,
                worker_name,
                assigned_by=assigned_by,
                summary=summary,
                file_paths=file_paths,
            )

    async def list_swarm_assignments(self, swarm_id: str) -> list[dict]:
        """List all assignments for one swarm."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.list_assignments(conn, swarm_id)

    async def record_swarm_message(
        self,
        swarm_id: str,
        *,
        target_kind: str,
        from_entity: str,
        message: str,
        delivered: int,
        failed: int,
        total: int,
        target_role: str | None = None,
        target_worker_name: str | None = None,
    ) -> dict:
        """Persist one swarm message log entry."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.record_swarm_message(
                conn,
                swarm_id,
                target_kind=target_kind,
                from_entity=from_entity,
                message=message,
                delivered=delivered,
                failed=failed,
                total=total,
                target_role=target_role,
                target_worker_name=target_worker_name,
            )

    async def list_swarm_messages(self, swarm_id: str) -> list[dict]:
        """List all recorded messages for one swarm."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.list_swarm_messages(conn, swarm_id)

    async def reconcile_swarm_worker_sessions(self, active_sessions: set[str]) -> int:
        """Mark lost swarm worker sessions failed when they disappear mid-swarm."""
        async with self._engine.begin() as conn:
            return await _swarm_repo.reconcile_swarm_worker_sessions(conn, active_sessions)

    # --- Governance tracks (class-based repo with session factory) ---

    @property
    def governance(self):
        """Lazy-initialized governance track repository."""
        if not hasattr(self, "_governance_repo"):
            from sqlalchemy.ext.asyncio import async_sessionmaker

            from agent_backbone.services.database._governance_repo import GovernanceRepo

            session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
            self._governance_repo = GovernanceRepo(session_factory)
        return self._governance_repo
