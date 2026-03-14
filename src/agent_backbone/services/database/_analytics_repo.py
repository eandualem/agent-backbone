"""Analytics query repository — read-only queries over agent_activity with swarm joins.

Returns raw row dicts for Python-side aggregation (no SQL json_extract).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def query_analytics_rows(
    conn: AsyncConnection,
    *,
    sessions: Sequence[str],
    since: float | None = None,
    until: float | None = None,
    events: Sequence[str] | None = None,
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
    """Fetch activity rows for one or more sessions with optional filters.

    Returns rows ordered by (ts ASC, id ASC) for stable cursor pagination.
    """
    query = "SELECT * FROM agent_activity WHERE 1=1"
    params: dict[str, object] = {}

    # Session filter (always provided for analytics)
    if len(sessions) == 1:
        query += " AND session = :sess_0"
        params["sess_0"] = sessions[0]
    else:
        placeholders = []
        for idx, s in enumerate(sessions):
            key = f"sess_{idx}"
            params[key] = s
            placeholders.append(f":{key}")
        query += f" AND session IN ({', '.join(placeholders)})"

    if since is not None:
        query += " AND CAST(ts AS DOUBLE PRECISION) >= :since"
        params["since"] = since

    if until is not None:
        query += " AND CAST(ts AS DOUBLE PRECISION) <= :until"
        params["until"] = until

    if events:
        ev_placeholders = []
        for idx, ev in enumerate(events):
            key = f"ev_{idx}"
            params[key] = ev
            ev_placeholders.append(f":{key}")
        query += f" AND event IN ({', '.join(ev_placeholders)})"

    if runtime is not None:
        query += " AND runtime = :runtime"
        params["runtime"] = runtime

    if model is not None:
        query += " AND model = :model"
        params["model"] = model

    if source_kind is not None:
        query += " AND source_kind = :source_kind"
        params["source_kind"] = source_kind

    if source_ref is not None:
        query += " AND source_ref = :source_ref"
        params["source_ref"] = source_ref

    if trace_id is not None:
        query += " AND trace_id = :trace_id"
        params["trace_id"] = trace_id

    if parent_trace_id is not None:
        query += " AND parent_trace_id = :parent_trace_id"
        params["parent_trace_id"] = parent_trace_id

    # Cursor pagination: (ts, id) > (cursor_ts, cursor_id)
    if cursor_ts is not None and cursor_id is not None:
        query += (
            " AND (CAST(ts AS DOUBLE PRECISION) > :cursor_ts"
            " OR (CAST(ts AS DOUBLE PRECISION) = :cursor_ts AND id > :cursor_id))"
        )
        params["cursor_ts"] = float(cursor_ts)
        params["cursor_id"] = cursor_id

    query += " ORDER BY CAST(ts AS DOUBLE PRECISION) ASC, id ASC"

    if limit is not None:
        query += " LIMIT :lim"
        params["lim"] = limit

    result = await conn.execute(text(query), params)
    return [dict(row._mapping) for row in result.fetchall()]


async def get_swarm_sessions_for_agent(
    conn: AsyncConnection,
    coding_agent_session: str,
    *,
    swarm_id: str | None = None,
    worker_name: str | None = None,
    worker_role: str | None = None,
) -> list[dict]:
    """Find swarm worker sessions linked to a coding agent session.

    Returns dicts with: session, swarm_id, worker_name, worker_role,
    coding_agent_session, repo, task_id.
    """
    query = """
        SELECT
            sw.session,
            sw.swarm_id,
            sw.name AS worker_name,
            sw.role AS worker_role,
            s.coding_agent_session,
            s.repo,
            s.task_id
        FROM swarm_workers sw
        JOIN swarms s ON sw.swarm_id = s.swarm_id
        WHERE s.coding_agent_session = :cas
    """
    params: dict[str, object] = {"cas": coding_agent_session}

    if swarm_id is not None:
        query += " AND sw.swarm_id = :swarm_id"
        params["swarm_id"] = swarm_id

    if worker_name is not None:
        query += " AND sw.name = :worker_name"
        params["worker_name"] = worker_name

    if worker_role is not None:
        query += " AND sw.role = :worker_role"
        params["worker_role"] = worker_role

    result = await conn.execute(text(query), params)
    return [dict(row._mapping) for row in result.fetchall()]


async def get_worker_swarm_attribution(
    conn: AsyncConnection,
    worker_session: str,
) -> dict | None:
    """Look up swarm context for a session that is itself a worker.

    Returns None if the session is not a swarm worker.
    """
    query = """
        SELECT
            sw.session,
            sw.swarm_id,
            sw.name AS worker_name,
            sw.role AS worker_role,
            s.coding_agent_session,
            s.repo,
            s.task_id
        FROM swarm_workers sw
        JOIN swarms s ON sw.swarm_id = s.swarm_id
        WHERE sw.session = :ws
        LIMIT 1
    """
    result = await conn.execute(text(query), {"ws": worker_session})
    row = result.fetchone()
    return dict(row._mapping) if row else None
