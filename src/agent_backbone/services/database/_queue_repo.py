"""Queue, dedup, dependencies, acknowledgments, and heartbeat repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- Dedup ---


async def is_duplicate_delivery_id(
    conn: AsyncConnection,
    delivery_id: str,
) -> bool:
    """Check if a delivery ID has been seen before."""
    if not delivery_id:
        return False
    result = await conn.execute(
        text("SELECT 1 FROM dedup_log WHERE delivery_id = :delivery_id"),
        {"delivery_id": delivery_id},
    )
    return result.fetchone() is not None


async def record_delivery_id(
    conn: AsyncConnection,
    delivery_id: str,
) -> None:
    """Record a delivery ID for dedup."""
    if not delivery_id:
        return
    await conn.execute(
        text(
            """INSERT INTO dedup_log (delivery_id, received_at)
               VALUES (:delivery_id, :received_at)
               ON CONFLICT DO NOTHING"""
        ),
        {"delivery_id": delivery_id, "received_at": _now_iso()},
    )


async def prune_delivery_ids(
    conn: AsyncConnection,
    max_age_hours: int = 24,
) -> int:
    """Remove old dedup entries. Returns count deleted."""
    cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
    result = await conn.execute(
        text("DELETE FROM dedup_log WHERE received_at < :cutoff"),
        {"cutoff": cutoff},
    )
    return result.rowcount


# --- Issue dependencies ---


async def upsert_dependency(
    conn: AsyncConnection,
    parent: int,
    sub: int,
) -> None:
    """Record a parent->sub-issue dependency."""
    await conn.execute(
        text(
            """INSERT INTO issue_dependencies (parent_number, sub_issue_number, updated_at)
               VALUES (:parent, :sub, :now)
               ON CONFLICT(parent_number, sub_issue_number) DO UPDATE SET
                 updated_at = excluded.updated_at"""
        ),
        {"parent": parent, "sub": sub, "now": _now_iso()},
    )


async def get_parents(
    conn: AsyncConnection,
    sub_issue_number: int,
) -> list[int]:
    """Get parent issue numbers for a given sub-issue."""
    result = await conn.execute(
        text("SELECT parent_number FROM issue_dependencies WHERE sub_issue_number = :sub"),
        {"sub": sub_issue_number},
    )
    rows = result.fetchall()
    return [row._mapping["parent_number"] for row in rows]


async def sync_dependencies(
    conn: AsyncConnection,
    parent: int,
    sub_issues: list[int],
) -> None:
    """Sync the dependency table for a parent."""
    now = _now_iso()
    for sub in sub_issues:
        await conn.execute(
            text(
                """INSERT INTO issue_dependencies (parent_number, sub_issue_number, updated_at)
                   VALUES (:parent, :sub, :now)
                   ON CONFLICT(parent_number, sub_issue_number) DO UPDATE SET
                     updated_at = excluded.updated_at"""
            ),
            {"parent": parent, "sub": sub, "now": now},
        )
    if sub_issues:
        # Build parameterized IN clause
        param_names = [f":sub_{i}" for i in range(len(sub_issues))]
        placeholders = ",".join(param_names)
        params: dict[str, object] = {"parent": parent}
        for i, sub in enumerate(sub_issues):
            params[f"sub_{i}"] = sub
        await conn.execute(
            text(
                f"DELETE FROM issue_dependencies WHERE parent_number = :parent "
                f"AND sub_issue_number NOT IN ({placeholders})"
            ),
            params,
        )
    else:
        await conn.execute(
            text("DELETE FROM issue_dependencies WHERE parent_number = :parent"),
            {"parent": parent},
        )


# --- Acknowledgments ---


async def record_acknowledgment(
    conn: AsyncConnection,
    issue_number: int,
    target_entity: str,
) -> None:
    """Record that an entity has acknowledged an issue."""
    await conn.execute(
        text(
            """INSERT INTO acknowledgments
               (issue_number, target_entity, acknowledged_at)
               VALUES (:issue_number, :target_entity, :acknowledged_at)
               ON CONFLICT(issue_number, target_entity) DO UPDATE SET
                 acknowledged_at = excluded.acknowledged_at"""
        ),
        {
            "issue_number": issue_number,
            "target_entity": target_entity,
            "acknowledged_at": _now_iso(),
        },
    )


async def is_acknowledged(
    conn: AsyncConnection,
    issue_number: int,
    target_entity: str,
) -> bool:
    """Check if entity has acknowledged this issue."""
    result = await conn.execute(
        text(
            "SELECT 1 FROM acknowledgments"
            " WHERE issue_number = :issue_number AND target_entity = :target_entity"
        ),
        {"issue_number": issue_number, "target_entity": target_entity},
    )
    return result.fetchone() is not None


async def clear_acknowledgment(
    conn: AsyncConnection,
    issue_number: int,
    target_entity: str,
) -> None:
    """Clear acknowledgment for entity on issue."""
    await conn.execute(
        text(
            "DELETE FROM acknowledgments"
            " WHERE issue_number = :issue_number AND target_entity = :target_entity"
        ),
        {"issue_number": issue_number, "target_entity": target_entity},
    )


# --- Heartbeats ---


async def record_heartbeat(
    conn: AsyncConnection,
    agent: str,
    outcome: str,
    message: str | None = None,
) -> int:
    """Record a heartbeat delivery attempt. Returns the row ID."""
    result = await conn.execute(
        text(
            """INSERT INTO heartbeats (agent, delivered_at, outcome, message)
               VALUES (:agent, :delivered_at, :outcome, :message)
               RETURNING id"""
        ),
        {
            "agent": agent,
            "delivered_at": _now_iso(),
            "outcome": outcome,
            "message": message,
        },
    )
    return result.scalar_one()


async def get_last_heartbeat(
    conn: AsyncConnection,
    agent: str,
    outcome: str = "delivered",
) -> str | None:
    """Get the delivered_at ISO string of the most recent matching heartbeat."""
    result = await conn.execute(
        text(
            """SELECT delivered_at FROM heartbeats
               WHERE agent = :agent AND outcome = :outcome
               ORDER BY delivered_at DESC LIMIT 1"""
        ),
        {"agent": agent, "outcome": outcome},
    )
    row = result.fetchone()
    return row._mapping["delivered_at"] if row else None


async def query_heartbeats(
    conn: AsyncConnection,
    agent: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query heartbeat records with optional filters."""
    conditions: list[str] = []
    params: dict[str, object] = {}
    if agent is not None:
        conditions.append("agent = :agent")
        params["agent"] = agent
    if outcome is not None:
        conditions.append("outcome = :outcome")
        params["outcome"] = outcome

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"SELECT * FROM heartbeats {where} ORDER BY delivered_at DESC LIMIT :lim"
    params["lim"] = limit

    result = await conn.execute(text(sql), params)
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]


# --- Message queue ---


async def enqueue_message(
    conn: AsyncConnection,
    session_name: str,
    message: str,
    issue_number: int | None = None,
    target_entity: str | None = None,
    delivery_kind: str = "issue",
    flow_name: str = "",
) -> int:
    """Enqueue a message for later delivery. Returns the row ID."""
    result = await conn.execute(
        text(
            """INSERT INTO message_queue
               (session_name, message, issue_number, target_entity,
                delivery_kind, flow_name, enqueued_at, status)
               VALUES (:session_name, :message, :issue_number, :target_entity,
                       :delivery_kind, :flow_name, :enqueued_at, 'pending')
               RETURNING id"""
        ),
        {
            "session_name": session_name,
            "message": message,
            "issue_number": issue_number,
            "target_entity": target_entity,
            "delivery_kind": delivery_kind,
            "flow_name": flow_name,
            "enqueued_at": _now_iso(),
        },
    )
    return result.scalar_one()


async def dequeue_messages(
    conn: AsyncConnection,
    session_name: str,
    limit: int = 10,
) -> list[dict]:
    """Get pending messages for a session, oldest first."""
    result = await conn.execute(
        text(
            """SELECT * FROM message_queue
               WHERE session_name = :session_name AND status = 'pending'
               ORDER BY enqueued_at ASC LIMIT :lim"""
        ),
        {"session_name": session_name, "lim": limit},
    )
    rows = result.fetchall()
    return [dict(row._mapping) for row in rows]


async def mark_message_delivered(
    conn: AsyncConnection,
    message_id: int,
) -> None:
    """Mark a queued message as delivered."""
    await conn.execute(
        text(
            """UPDATE message_queue
               SET status = 'delivered', delivered_at = :delivered_at
               WHERE id = :id"""
        ),
        {"delivered_at": _now_iso(), "id": message_id},
    )


async def expire_stale_pending(
    conn: AsyncConnection,
    max_age_minutes: int = 30,
) -> int:
    """Mark pending messages older than max_age_minutes as expired.

    Prevents stale messages from looping indefinitely in the drain cycle.
    Returns the number of expired messages.
    """
    result = await conn.execute(
        text(
            """UPDATE message_queue
               SET status = 'expired', delivered_at = :now
               WHERE status = 'pending'
                 AND enqueued_at < :cutoff"""
        ),
        {
            "now": _now_iso(),
            "cutoff": (datetime.now(UTC) - timedelta(minutes=max_age_minutes)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
        },
    )
    return result.rowcount


async def purge_pending_for_issue(
    conn: AsyncConnection,
    issue_number: int,
) -> int:
    """Mark all pending messages for an issue as delivered (issue closed).

    Returns the number of purged messages.
    """
    result = await conn.execute(
        text(
            """UPDATE message_queue
               SET status = 'delivered', delivered_at = :delivered_at
               WHERE issue_number = :issue_number AND status = 'pending'"""
        ),
        {"delivered_at": _now_iso(), "issue_number": issue_number},
    )
    return result.rowcount
