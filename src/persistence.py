"""SQLite persistence layer for delivery tracking, dedup, and agent state.

Uses aiosqlite for async access. BackboneDB is an async context manager
that handles connection lifecycle and schema initialization.

Default path: ~/.prefect/backbone.db (configurable via BackboneConfig.delivery.db_path).
Tests use `:memory:` for isolation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    target_entity TEXT NOT NULL,
    session_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    flow_name TEXT NOT NULL DEFAULT '',
    flow_run_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS dedup_log (
    delivery_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS agent_states (
    session_name TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'unknown',
    current_issue INTEGER,
    last_activity TEXT,
    started_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS issue_dependencies (
    parent_number INTEGER NOT NULL,
    sub_issue_number INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (parent_number, sub_issue_number)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_issue ON deliveries(issue_number);
CREATE INDEX IF NOT EXISTS idx_deliveries_entity ON deliveries(target_entity);
CREATE INDEX IF NOT EXISTS idx_deliveries_outcome ON deliveries(outcome);
CREATE INDEX IF NOT EXISTS idx_deliveries_created ON deliveries(created_at);
CREATE TABLE IF NOT EXISTS acknowledgments (
    issue_number INTEGER NOT NULL,
    target_entity TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL,
    PRIMARY KEY (issue_number, target_entity)
);

CREATE INDEX IF NOT EXISTS idx_deps_sub ON issue_dependencies(sub_issue_number);

CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_heartbeats_agent ON heartbeats(agent);

CREATE TABLE IF NOT EXISTS message_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    message TEXT NOT NULL,
    issue_number INTEGER,
    target_entity TEXT,
    flow_name TEXT DEFAULT '',
    enqueued_at TEXT NOT NULL,
    delivered_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_mq_status ON message_queue(status);
CREATE INDEX IF NOT EXISTS idx_mq_session ON message_queue(session_name);
"""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class BackboneDB:
    """Async SQLite database for backbone persistence.

    Usage:
        async with BackboneDB(db_path) as db:
            await db.record_delivery(...)
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> BackboneDB:
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "BackboneDB not initialized — use async with"
        return self._conn

    # --- Delivery tracking ---

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
        cursor = await self.conn.execute(
            """INSERT INTO deliveries
               (issue_number, target_entity, session_name,
                outcome, flow_name, flow_run_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                issue_number,
                target_entity,
                session_name,
                outcome,
                flow_name,
                flow_run_id,
                _now_iso(),
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def query_deliveries(
        self,
        issue_number: int | None = None,
        target_entity: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query delivery records with optional filters."""
        conditions: list[str] = []
        params: list[object] = []
        if issue_number is not None:
            conditions.append("issue_number = ?")
            params.append(issue_number)
        if target_entity is not None:
            conditions.append("target_entity = ?")
            params.append(target_entity)
        if outcome is not None:
            conditions.append("outcome = ?")
            params.append(outcome)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM deliveries {where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_failed_deliveries(self, limit: int = 50) -> list[dict]:
        """Get deliveries with failed outcomes for retry."""
        cursor = await self.conn.execute(
            """SELECT * FROM deliveries
               WHERE outcome IN ('offline', 'delivery_failed', 'deferred')
               ORDER BY created_at ASC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def prune_old_deliveries(self, retention_days: int = 30) -> int:
        """Delete delivery records older than retention period. Returns count deleted."""
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        cursor = await self.conn.execute("DELETE FROM deliveries WHERE created_at < ?", (cutoff,))
        await self.conn.commit()
        return cursor.rowcount

    # --- Dedup ---

    async def is_duplicate_delivery_id(self, delivery_id: str) -> bool:
        """Check if a delivery ID has been seen before."""
        if not delivery_id:
            return False
        cursor = await self.conn.execute(
            "SELECT 1 FROM dedup_log WHERE delivery_id = ?", (delivery_id,)
        )
        return await cursor.fetchone() is not None

    async def record_delivery_id(self, delivery_id: str) -> None:
        """Record a delivery ID for dedup."""
        if not delivery_id:
            return
        await self.conn.execute(
            "INSERT OR IGNORE INTO dedup_log (delivery_id, received_at) VALUES (?, ?)",
            (delivery_id, _now_iso()),
        )
        await self.conn.commit()

    async def prune_delivery_ids(self, max_age_hours: int = 24) -> int:
        """Remove old dedup entries. Returns count deleted."""
        cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
        cursor = await self.conn.execute("DELETE FROM dedup_log WHERE received_at < ?", (cutoff,))
        await self.conn.commit()
        return cursor.rowcount

    # --- Agent state ---

    async def get_agent_state(self, session_name: str) -> dict | None:
        """Get the current state record for an agent session."""
        cursor = await self.conn.execute(
            "SELECT * FROM agent_states WHERE session_name = ?", (session_name,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def set_agent_state(
        self,
        session_name: str,
        state: str,
        current_issue: int | None = None,
        last_activity: str | None = None,
        started_at: str | None = None,
    ) -> None:
        """Upsert agent state."""
        now = _now_iso()
        await self.conn.execute(
            """INSERT INTO agent_states
               (session_name, state, current_issue,
                last_activity, started_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_name) DO UPDATE SET
                 state = excluded.state,
                 current_issue = excluded.current_issue,
                 last_activity = COALESCE(
                     excluded.last_activity,
                     agent_states.last_activity),
                 started_at = COALESCE(
                     excluded.started_at,
                     agent_states.started_at),
                 updated_at = excluded.updated_at""",
            (session_name, state, current_issue, last_activity, started_at, now),
        )
        await self.conn.commit()

    async def get_all_agent_states(self) -> list[dict]:
        """Get state records for all tracked agents."""
        cursor = await self.conn.execute("SELECT * FROM agent_states ORDER BY session_name")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # --- Issue dependencies ---

    async def upsert_dependency(self, parent: int, sub: int) -> None:
        """Record a parent→sub-issue dependency."""
        await self.conn.execute(
            """INSERT INTO issue_dependencies (parent_number, sub_issue_number, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(parent_number, sub_issue_number) DO UPDATE SET
                 updated_at = excluded.updated_at""",
            (parent, sub, _now_iso()),
        )
        await self.conn.commit()

    async def get_parents(self, sub_issue_number: int) -> list[int]:
        """Get parent issue numbers for a given sub-issue."""
        cursor = await self.conn.execute(
            "SELECT parent_number FROM issue_dependencies WHERE sub_issue_number = ?",
            (sub_issue_number,),
        )
        rows = await cursor.fetchall()
        return [row["parent_number"] for row in rows]

    async def sync_dependencies(self, parent: int, sub_issues: list[int]) -> None:
        """Sync the dependency table for a parent: upsert all current sub-issues,
        remove stale ones no longer in the list."""
        now = _now_iso()
        for sub in sub_issues:
            await self.conn.execute(
                """INSERT INTO issue_dependencies (parent_number, sub_issue_number, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(parent_number, sub_issue_number) DO UPDATE SET
                     updated_at = excluded.updated_at""",
                (parent, sub, now),
            )
        # Remove sub-issues no longer in the list
        if sub_issues:
            placeholders = ",".join("?" * len(sub_issues))
            await self.conn.execute(
                f"DELETE FROM issue_dependencies WHERE parent_number = ? "
                f"AND sub_issue_number NOT IN ({placeholders})",
                [parent, *sub_issues],
            )
        else:
            await self.conn.execute(
                "DELETE FROM issue_dependencies WHERE parent_number = ?",
                (parent,),
            )
        await self.conn.commit()

    # --- Acknowledgments ---

    async def record_acknowledgment(self, issue_number: int, target_entity: str) -> None:
        """Record that an entity has acknowledged an issue (commented on it)."""
        await self.conn.execute(
            """INSERT OR REPLACE INTO acknowledgments
               (issue_number, target_entity, acknowledged_at)
               VALUES (?, ?, ?)""",
            (issue_number, target_entity, _now_iso()),
        )
        await self.conn.commit()

    async def is_acknowledged(self, issue_number: int, target_entity: str) -> bool:
        """Check if entity has acknowledged this issue."""
        cursor = await self.conn.execute(
            "SELECT 1 FROM acknowledgments WHERE issue_number = ? AND target_entity = ?",
            (issue_number, target_entity),
        )
        return await cursor.fetchone() is not None

    async def clear_acknowledgment(self, issue_number: int, target_entity: str) -> None:
        """Clear acknowledgment for entity on issue (new info arrived)."""
        await self.conn.execute(
            "DELETE FROM acknowledgments WHERE issue_number = ? AND target_entity = ?",
            (issue_number, target_entity),
        )
        await self.conn.commit()

    # --- Heartbeats ---

    async def record_heartbeat(self, agent: str, outcome: str, message: str | None = None) -> int:
        """Record a heartbeat delivery attempt. Returns the row ID."""
        cursor = await self.conn.execute(
            """INSERT INTO heartbeats (agent, delivered_at, outcome, message)
               VALUES (?, ?, ?, ?)""",
            (agent, _now_iso(), outcome, message),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_last_heartbeat(self, agent: str, outcome: str = "delivered") -> str | None:
        """Get the delivered_at ISO string of the most recent matching heartbeat."""
        cursor = await self.conn.execute(
            """SELECT delivered_at FROM heartbeats
               WHERE agent = ? AND outcome = ?
               ORDER BY delivered_at DESC LIMIT 1""",
            (agent, outcome),
        )
        row = await cursor.fetchone()
        return row["delivered_at"] if row else None

    # --- Message queue ---

    async def enqueue_message(
        self,
        session_name: str,
        message: str,
        issue_number: int | None = None,
        target_entity: str | None = None,
        flow_name: str = "",
    ) -> int:
        """Enqueue a message for later delivery. Returns the row ID."""
        cursor = await self.conn.execute(
            """INSERT INTO message_queue
               (session_name, message, issue_number, target_entity,
                flow_name, enqueued_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (session_name, message, issue_number, target_entity, flow_name, _now_iso()),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def dequeue_messages(
        self,
        session_name: str,
        limit: int = 10,
    ) -> list[dict]:
        """Get pending messages for a session, oldest first."""
        cursor = await self.conn.execute(
            """SELECT * FROM message_queue
               WHERE session_name = ? AND status = 'pending'
               ORDER BY enqueued_at ASC LIMIT ?""",
            (session_name, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_message_delivered(self, message_id: int) -> None:
        """Mark a queued message as delivered."""
        await self.conn.execute(
            """UPDATE message_queue
               SET status = 'delivered', delivered_at = ?
               WHERE id = ?""",
            (_now_iso(), message_id),
        )
        await self.conn.commit()

    async def query_heartbeats(
        self,
        agent: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query heartbeat records with optional filters."""
        conditions: list[str] = []
        params: list[object] = []
        if agent is not None:
            conditions.append("agent = ?")
            params.append(agent)
        if outcome is not None:
            conditions.append("outcome = ?")
            params.append(outcome)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM heartbeats {where} ORDER BY delivered_at DESC LIMIT ?"
        params.append(limit)

        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
