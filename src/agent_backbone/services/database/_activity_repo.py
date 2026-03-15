"""Agent activity repository."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _activity_params(
    *,
    session: str,
    event: str,
    data: str | None,
    ts: str,
    entity: str | None,
    runtime: str | None,
    source_kind: str | None,
    source_ref: str | None,
    source_event_id: str | None,
    trace_id: str | None,
    parent_trace_id: str | None,
    model: str | None,
) -> dict[str, str | None]:
    return {
        "session": session,
        "event": event,
        "entity": entity,
        "runtime": runtime,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "source_event_id": source_event_id,
        "trace_id": trace_id,
        "parent_trace_id": parent_trace_id,
        "model": model,
        "data": data,
        "ts": ts,
        "received_at": _now_iso(),
    }


async def record_activity(
    conn: AsyncConnection,
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
    """Insert an activity event. Returns the row ID."""
    result = await conn.execute(
        text(
            """INSERT INTO agent_activity (
                   session,
                   event,
                   entity,
                   runtime,
                   source_kind,
                   source_ref,
                   source_event_id,
                   trace_id,
                   parent_trace_id,
                   model,
                   data,
                   ts,
                   received_at
               )
               VALUES (
                   :session,
                   :event,
                   :entity,
                   :runtime,
                   :source_kind,
                   :source_ref,
                   :source_event_id,
                   :trace_id,
                   :parent_trace_id,
                   :model,
                   :data,
                   :ts,
                   :received_at
               )
               RETURNING id"""
        ),
        _activity_params(
            session=session,
            event=event,
            data=data,
            ts=ts,
            entity=entity,
            runtime=runtime,
            source_kind=source_kind,
            source_ref=source_ref,
            source_event_id=source_event_id,
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            model=model,
        ),
    )
    return result.scalar_one()


async def record_activity_batch(
    conn: AsyncConnection,
    rows: Sequence[dict[str, str | None]],
) -> int:
    """Insert a batch of activity events. Returns the number of inserted rows."""
    if not rows:
        return 0

    await conn.execute(
        text(
            """INSERT INTO agent_activity (
                   session,
                   event,
                   entity,
                   runtime,
                   source_kind,
                   source_ref,
                   source_event_id,
                   trace_id,
                   parent_trace_id,
                   model,
                   data,
                   ts,
                   received_at
               )
               VALUES (
                   :session,
                   :event,
                   :entity,
                   :runtime,
                   :source_kind,
                   :source_ref,
                   :source_event_id,
                   :trace_id,
                   :parent_trace_id,
                   :model,
                   :data,
                   :ts,
                   :received_at
               )"""
        ),
        list(rows),
    )
    return len(rows)


async def get_activity(
    conn: AsyncConnection,
    session: str,
    limit: int = 50,
    since: str | None = None,
) -> list[dict]:
    """Query activity events for a session, newest first."""
    query = """SELECT * FROM agent_activity
           WHERE session = :session"""
    params: dict[str, str | int] = {"session": session, "lim": limit}

    if since is not None:
        query += " AND CAST(ts AS DOUBLE PRECISION) >= :since"
        params["since"] = float(since)

    query += " ORDER BY CAST(ts AS DOUBLE PRECISION) DESC, id DESC LIMIT :lim"
    result = await conn.execute(text(query), params)
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]


async def query_activity(
    conn: AsyncConnection,
    *,
    limit: int = 50,
    events: Sequence[str] | None = None,
) -> list[dict]:
    """Query activity events across all sessions, newest first."""
    query = """SELECT * FROM agent_activity WHERE 1 = 1"""
    params: dict[str, str | int] = {"lim": limit}

    if events:
        placeholders = []
        for idx, event in enumerate(events):
            key = f"event_{idx}"
            params[key] = event
            placeholders.append(f":{key}")
        query += f" AND event IN ({', '.join(placeholders)})"

    query += " ORDER BY CAST(ts AS DOUBLE PRECISION) DESC, id DESC LIMIT :lim"
    result = await conn.execute(text(query), params)
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]


async def has_activity_event(
    conn: AsyncConnection,
    *,
    session: str,
    event: str,
    source_ref: str | None = None,
    since: float | None = None,
) -> bool:
    """Whether a matching activity event exists for the session."""
    query = """SELECT 1 FROM agent_activity WHERE session = :session AND event = :event"""
    params: dict[str, str] = {
        "session": session,
        "event": event,
    }

    if source_ref is not None:
        query += " AND source_ref = :source_ref"
        params["source_ref"] = source_ref

    if since is not None:
        query += " AND CAST(ts AS DOUBLE PRECISION) >= :since"
        params["since"] = since

    query += " LIMIT 1"
    result = await conn.execute(text(query), params)
    return result.fetchone() is not None
