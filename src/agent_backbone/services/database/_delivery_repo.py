"""Delivery tracking repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def record_delivery(
    conn: AsyncConnection,
    issue_number: int,
    target_entity: str,
    session_name: str,
    outcome: str,
    flow_name: str = "",
    flow_run_id: str = "",
) -> int:
    """Record a delivery attempt. Returns the row ID."""
    result = await conn.execute(
        text(
            """INSERT INTO deliveries
               (issue_number, target_entity, session_name,
                outcome, flow_name, flow_run_id, created_at)
               VALUES (:issue_number, :target_entity, :session_name,
                       :outcome, :flow_name, :flow_run_id, :created_at)
               RETURNING id"""
        ),
        {
            "issue_number": issue_number,
            "target_entity": target_entity,
            "session_name": session_name,
            "outcome": outcome,
            "flow_name": flow_name,
            "flow_run_id": flow_run_id,
            "created_at": _now_iso(),
        },
    )
    return result.scalar_one()


async def claim_delivery_attempt(
    conn: AsyncConnection,
    issue_number: int,
    target_entity: str,
    session_name: str,
    flow_name: str,
) -> int | None:
    """Reserve an issue delivery slot before sending to avoid duplicate sends."""
    result = await conn.execute(
        text(
            """INSERT INTO deliveries
               (issue_number, target_entity, session_name, outcome, flow_name, created_at)
               VALUES (:issue_number, :target_entity, :session_name, 'attempting', :flow_name, :now)
               ON CONFLICT (issue_number, session_name)
               WHERE outcome IN ('attempting','delivered','retried')
               DO NOTHING
               RETURNING id"""
        ),
        {
            "issue_number": issue_number,
            "target_entity": target_entity,
            "session_name": session_name,
            "flow_name": flow_name,
            "now": _now_iso(),
        },
    )
    row = result.fetchone()
    return row._mapping["id"] if row else None


async def finalize_delivery_attempt(
    conn: AsyncConnection,
    delivery_id: int,
    outcome: str,
) -> None:
    """Finalize a claimed delivery attempt."""
    await conn.execute(
        text(
            """UPDATE deliveries SET outcome = :outcome
               WHERE id = :id AND outcome = 'attempting'"""
        ),
        {"id": delivery_id, "outcome": outcome},
    )


async def reclaim_stale_attempts(
    conn: AsyncConnection,
    max_age_minutes: int = 5,
) -> int:
    """Delete stale attempting rows so new delivery claims can proceed."""
    cutoff = (datetime.now(UTC) - timedelta(minutes=max_age_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    result = await conn.execute(
        text(
            """DELETE FROM deliveries
               WHERE outcome = 'attempting' AND created_at < :cutoff"""
        ),
        {"cutoff": cutoff},
    )
    return result.rowcount


async def query_deliveries(
    conn: AsyncConnection,
    issue_number: int | None = None,
    target_entity: str | None = None,
    session_name: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query delivery records with optional filters."""
    conditions: list[str] = []
    params: dict[str, object] = {}
    if issue_number is not None:
        conditions.append("issue_number = :issue_number")
        params["issue_number"] = issue_number
    if target_entity is not None:
        conditions.append("target_entity = :target_entity")
        params["target_entity"] = target_entity
    if session_name is not None:
        conditions.append("session_name = :session_name")
        params["session_name"] = session_name
    if outcome is not None:
        conditions.append("outcome = :outcome")
        params["outcome"] = outcome

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"SELECT * FROM deliveries {where} ORDER BY created_at DESC LIMIT :lim"
    params["lim"] = limit

    result = await conn.execute(text(sql), params)
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]


async def get_failed_deliveries(
    conn: AsyncConnection,
    limit: int = 50,
) -> list[dict]:
    """Get deliveries with failed outcomes for retry."""
    result = await conn.execute(
        text(
            """SELECT d.* FROM deliveries d
               WHERE d.outcome IN (
                   'offline', 'delivery_failed', 'deferred',
                   'copy_mode', 'user_interacting', 'unknown_state',
                   'agent_working', 'plan_waiting', 'permission_waiting', 'grace_period'
               )
                 AND NOT EXISTS (
                   SELECT 1 FROM deliveries d2
                   WHERE d2.issue_number = d.issue_number
                     AND d2.target_entity = d.target_entity
                     AND (d2.outcome LIKE '%delivered' OR d2.outcome LIKE '%retried')
                     AND d2.created_at > d.created_at
                 )
               ORDER BY d.created_at ASC LIMIT :lim"""
        ),
        {"lim": limit},
    )
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]


async def prune_old_deliveries(
    conn: AsyncConnection,
    retention_days: int = 30,
) -> int:
    """Delete delivery records older than retention period. Returns count deleted."""
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
    result = await conn.execute(
        text("DELETE FROM deliveries WHERE created_at < :cutoff"),
        {"cutoff": cutoff},
    )
    return result.rowcount


async def get_delivery_stats(
    conn: AsyncConnection,
) -> list[dict]:
    """Get delivery counts grouped by outcome."""
    result = await conn.execute(
        text("SELECT outcome, COUNT(*) as cnt FROM deliveries GROUP BY outcome")
    )
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]
