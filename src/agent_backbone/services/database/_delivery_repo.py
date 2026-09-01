"""Delivery tracking repository.

Every delivery attempt — issue, comment, pull request, direct message,
watch notification — is recorded with its ``kind``. Issue-keyed rows carry
the repository so several repositories may share issue numbers.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from agent_backbone.models import RETRYABLE_OUTCOMES
from agent_backbone.services.database._time import cutoff_iso, now_iso


async def record_delivery(
    conn: AsyncConnection,
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
    result = await conn.execute(
        text(
            """INSERT INTO deliveries
               (kind, repo, issue_number, target_entity, session_name,
                outcome, flow_name, flow_run_id, preview, created_at)
               VALUES (:kind, :repo, :issue_number, :target_entity, :session_name,
                       :outcome, :flow_name, :flow_run_id, :preview, :created_at)
               RETURNING id"""
        ),
        {
            "kind": kind,
            "repo": repo,
            "issue_number": issue_number,
            "target_entity": target_entity,
            "session_name": session_name,
            "outcome": outcome,
            "flow_name": flow_name,
            "flow_run_id": flow_run_id,
            "preview": preview[:200],
            "created_at": now_iso(),
        },
    )
    return result.scalar_one()


async def claim_delivery_attempt(
    conn: AsyncConnection,
    issue_number: int,
    target_entity: str,
    session_name: str,
    flow_name: str,
    *,
    repo: str = "",
    preview: str = "",
) -> int | None:
    """Reserve an issue delivery slot before sending to avoid duplicate sends."""
    result = await conn.execute(
        text(
            """INSERT INTO deliveries
               (kind, repo, issue_number, target_entity, session_name, outcome,
                flow_name, preview, created_at)
               VALUES ('issue', :repo, :issue_number, :target_entity, :session_name,
                       'attempting', :flow_name, :preview, :now)
               ON CONFLICT (repo, issue_number, session_name)
               WHERE kind = 'issue'
                 AND issue_number IS NOT NULL
                 AND outcome IN ('attempting','delivered','retried')
               DO NOTHING
               RETURNING id"""
        ),
        {
            "repo": repo,
            "issue_number": issue_number,
            "target_entity": target_entity,
            "session_name": session_name,
            "flow_name": flow_name,
            "preview": preview[:200],
            "now": now_iso(),
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
    result = await conn.execute(
        text(
            """DELETE FROM deliveries
               WHERE outcome = 'attempting' AND created_at < :cutoff"""
        ),
        {"cutoff": cutoff_iso(minutes=max_age_minutes)},
    )
    return result.rowcount


async def query_deliveries(
    conn: AsyncConnection,
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
    conditions: list[str] = []
    params: dict[str, object] = {}
    if issue_number is not None:
        conditions.append("issue_number = :issue_number")
        params["issue_number"] = issue_number
    if repo is not None:
        conditions.append("repo = :repo")
        params["repo"] = repo
    if kind is not None:
        conditions.append("kind = :kind")
        params["kind"] = kind
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
    sql = f"SELECT * FROM deliveries {where} ORDER BY created_at DESC, id DESC LIMIT :lim"
    params["lim"] = limit

    result = await conn.execute(text(sql), params)
    return [dict(row._mapping) for row in result.fetchall()]


async def get_failed_deliveries(
    conn: AsyncConnection,
    limit: int = 50,
) -> list[dict]:
    """Issue deliveries whose latest outcome is retryable (no later success)."""
    placeholders = ",".join(f"'{o.value}'" for o in sorted(RETRYABLE_OUTCOMES))
    result = await conn.execute(
        text(
            f"""SELECT d.* FROM deliveries d
               WHERE d.kind = 'issue'
                 AND d.issue_number IS NOT NULL
                 AND d.outcome IN ({placeholders})
                 AND NOT EXISTS (
                   SELECT 1 FROM deliveries d2
                   WHERE d2.kind = 'issue'
                     AND d2.repo = d.repo
                     AND d2.issue_number = d.issue_number
                     AND d2.target_entity = d.target_entity
                     AND d2.outcome IN ('delivered', 'retried')
                     AND d2.created_at > d.created_at
                 )
               ORDER BY d.created_at ASC LIMIT :lim"""
        ),
        {"lim": limit},
    )
    return [dict(row._mapping) for row in result.fetchall()]


async def prune_old_deliveries(
    conn: AsyncConnection,
    retention_days: int = 30,
) -> int:
    """Delete delivery records older than retention period. Returns count deleted."""
    result = await conn.execute(
        text("DELETE FROM deliveries WHERE created_at < :cutoff"),
        {"cutoff": cutoff_iso(days=retention_days)},
    )
    return result.rowcount


async def get_delivery_stats(
    conn: AsyncConnection,
) -> list[dict]:
    """Get delivery counts grouped by outcome."""
    result = await conn.execute(
        text("SELECT outcome, COUNT(*) as cnt FROM deliveries GROUP BY outcome")
    )
    return [dict(row._mapping) for row in result.fetchall()]
