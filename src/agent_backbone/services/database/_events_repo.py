"""Inbound events repository — every webhook/poll/telegram/api event, before and after routing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def record_event(
    conn: AsyncConnection,
    *,
    delivery_id: str,
    source: str,
    event_type: str,
    repo: str = "",
    issue_number: int | None = None,
    sender: str = "",
    summary: str = "",
) -> int | None:
    """Insert an event; returns the row id, or None if the delivery id was already stored."""
    result = await conn.execute(
        text(
            """INSERT INTO events
               (delivery_id, source, repo, event_type, issue_number, sender, summary, received_at)
               VALUES (:delivery_id, :source, :repo, :event_type, :issue_number, :sender,
                       :summary, :received_at)
               ON CONFLICT (delivery_id) DO NOTHING
               RETURNING id"""
        ),
        {
            "delivery_id": delivery_id,
            "source": source,
            "repo": repo,
            "event_type": event_type,
            "issue_number": issue_number,
            "sender": sender,
            "summary": summary[:500],
            "received_at": _now_iso(),
        },
    )
    row = result.fetchone()
    return row._mapping["id"] if row else None


async def mark_event_processed(conn: AsyncConnection, event_id: int, outcome: str) -> None:
    await conn.execute(
        text("UPDATE events SET processed_at = :now, outcome = :outcome WHERE id = :id"),
        {"now": _now_iso(), "outcome": outcome[:500], "id": event_id},
    )


async def query_events(
    conn: AsyncConnection,
    *,
    repo: str | None = None,
    limit: int = 50,
) -> list[dict]:
    conditions: list[str] = []
    params: dict[str, object] = {"lim": limit}
    if repo is not None:
        conditions.append("repo = :repo")
        params["repo"] = repo
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    result = await conn.execute(
        text(f"SELECT * FROM events {where} ORDER BY received_at DESC, id DESC LIMIT :lim"),
        params,
    )
    return [dict(row._mapping) for row in result.fetchall()]


async def last_event_time_by_repo(conn: AsyncConnection) -> dict[str, str]:
    """Most recent ``received_at`` per repository (for status and backfill)."""
    result = await conn.execute(
        text("SELECT repo, MAX(received_at) AS last FROM events WHERE repo != '' GROUP BY repo")
    )
    return {row._mapping["repo"]: row._mapping["last"] for row in result.fetchall()}


async def prune_events(conn: AsyncConnection, retention_days: int = 30) -> int:
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
    result = await conn.execute(
        text("DELETE FROM events WHERE received_at < :cutoff"), {"cutoff": cutoff}
    )
    return result.rowcount
