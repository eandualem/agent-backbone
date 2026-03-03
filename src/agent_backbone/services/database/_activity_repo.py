"""Agent activity event repository."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def record_activity(
    conn: AsyncConnection,
    session: str,
    event: str,
    data: str | None,
    ts: str,
) -> int:
    """Insert an activity event. Returns the row ID."""
    result = await conn.execute(
        text(
            """INSERT INTO agent_activity (session, event, data, ts, received_at)
               VALUES (:session, :event, :data, :ts, :received_at)"""
        ),
        {
            "session": session,
            "event": event,
            "data": data,
            "ts": ts,
            "received_at": _now_iso(),
        },
    )
    return result.lastrowid  # type: ignore[return-value]


async def get_activity(
    conn: AsyncConnection,
    session: str,
    limit: int = 50,
    since: str | None = None,
) -> list[dict]:
    """Query activity events for a session, newest first."""
    if since:
        result = await conn.execute(
            text(
                """SELECT * FROM agent_activity
                   WHERE session = :session AND ts >= :since
                   ORDER BY ts DESC LIMIT :lim"""
            ),
            {"session": session, "since": since, "lim": limit},
        )
    else:
        result = await conn.execute(
            text(
                """SELECT * FROM agent_activity
                   WHERE session = :session
                   ORDER BY ts DESC LIMIT :lim"""
            ),
            {"session": session, "lim": limit},
        )
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]
