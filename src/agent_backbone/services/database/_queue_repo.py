"""Message queue, issue dependencies and acknowledgments (repository-keyed)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- Issue dependencies ---


async def get_parents(conn: AsyncConnection, sub_issue_number: int, *, repo: str = "") -> list[int]:
    result = await conn.execute(
        text(
            "SELECT parent_number FROM issue_dependencies"
            " WHERE repo = :repo AND sub_issue_number = :sub"
        ),
        {"repo": repo, "sub": sub_issue_number},
    )
    return [row._mapping["parent_number"] for row in result.fetchall()]


async def sync_dependencies(
    conn: AsyncConnection, parent: int, sub_issues: list[int], *, repo: str = ""
) -> None:
    now = _now_iso()
    for sub in sub_issues:
        await conn.execute(
            text(
                """INSERT INTO issue_dependencies
                   (repo, parent_number, sub_issue_number, updated_at)
                   VALUES (:repo, :parent, :sub, :now)
                   ON CONFLICT(repo, parent_number, sub_issue_number) DO UPDATE SET
                     updated_at = excluded.updated_at"""
            ),
            {"repo": repo, "parent": parent, "sub": sub, "now": now},
        )
    if sub_issues:
        placeholders = ",".join(f":sub_{i}" for i in range(len(sub_issues)))
        params: dict[str, object] = {"repo": repo, "parent": parent}
        for i, sub in enumerate(sub_issues):
            params[f"sub_{i}"] = sub
        await conn.execute(
            text(
                f"DELETE FROM issue_dependencies WHERE repo = :repo AND parent_number = :parent "
                f"AND sub_issue_number NOT IN ({placeholders})"
            ),
            params,
        )
    else:
        await conn.execute(
            text("DELETE FROM issue_dependencies WHERE repo = :repo AND parent_number = :parent"),
            {"repo": repo, "parent": parent},
        )


# --- Acknowledgments ---


async def record_acknowledgment(
    conn: AsyncConnection, issue_number: int, target_entity: str, *, repo: str = ""
) -> None:
    await conn.execute(
        text(
            """INSERT INTO acknowledgments (repo, issue_number, target_entity, acknowledged_at)
               VALUES (:repo, :issue_number, :target_entity, :acknowledged_at)
               ON CONFLICT(repo, issue_number, target_entity) DO UPDATE SET
                 acknowledged_at = excluded.acknowledged_at"""
        ),
        {
            "repo": repo,
            "issue_number": issue_number,
            "target_entity": target_entity,
            "acknowledged_at": _now_iso(),
        },
    )


async def is_acknowledged(
    conn: AsyncConnection, issue_number: int, target_entity: str, *, repo: str = ""
) -> bool:
    result = await conn.execute(
        text(
            "SELECT 1 FROM acknowledgments WHERE repo = :repo"
            " AND issue_number = :issue_number AND target_entity = :target_entity"
        ),
        {"repo": repo, "issue_number": issue_number, "target_entity": target_entity},
    )
    return result.fetchone() is not None


async def clear_acknowledgment(
    conn: AsyncConnection, issue_number: int, target_entity: str, *, repo: str = ""
) -> None:
    await conn.execute(
        text(
            "DELETE FROM acknowledgments WHERE repo = :repo"
            " AND issue_number = :issue_number AND target_entity = :target_entity"
        ),
        {"repo": repo, "issue_number": issue_number, "target_entity": target_entity},
    )


# --- Message queue ---

_INSERT_COLUMNS = """(session_name, message, repo, issue_number, target_entity,
                delivery_kind, flow_name, enqueued_at, status, content_hash)
               VALUES (:session_name, :message, :repo, :issue_number, :target_entity,
                       :delivery_kind, :flow_name, :enqueued_at, 'pending', :content_hash)"""


async def enqueue_message(
    conn: AsyncConnection,
    session_name: str,
    message: str,
    issue_number: int | None = None,
    target_entity: str | None = None,
    delivery_kind: str = "issue",
    flow_name: str = "",
    *,
    repo: str = "",
) -> int:
    """Enqueue a message for later delivery. Returns the row ID or -1 when deduped."""
    content_hash = hashlib.sha256(message.encode()).hexdigest()
    params = {
        "session_name": session_name,
        "message": message,
        "repo": repo,
        "issue_number": issue_number,
        "target_entity": target_entity,
        "delivery_kind": delivery_kind,
        "flow_name": flow_name,
        "enqueued_at": _now_iso(),
        "content_hash": content_hash,
    }

    if delivery_kind == "issue" and issue_number is not None:
        conflict = """ON CONFLICT (session_name, repo, issue_number)
               WHERE delivery_kind = 'issue'
                 AND status IN ('pending','in_progress')
                 AND issue_number IS NOT NULL
               DO NOTHING"""
    elif delivery_kind == "comment" and issue_number is not None:
        conflict = """ON CONFLICT (session_name, repo, issue_number, content_hash)
               WHERE delivery_kind = 'comment'
                 AND status IN ('pending','in_progress')
                 AND issue_number IS NOT NULL
               DO NOTHING"""
    elif delivery_kind == "direct_message":
        conflict = """ON CONFLICT (session_name, content_hash)
               WHERE delivery_kind = 'direct_message' AND status IN ('pending','in_progress')
               DO NOTHING"""
    else:
        conflict = ""

    sql = f"INSERT INTO message_queue {_INSERT_COLUMNS} {conflict} RETURNING id"
    result = await conn.execute(text(sql), params)
    row = result.fetchone()
    return row._mapping["id"] if row else -1


async def get_sessions_with_pending(conn: AsyncConnection) -> list[str]:
    result = await conn.execute(
        text("SELECT DISTINCT session_name FROM message_queue WHERE status = 'pending'")
    )
    return [row._mapping["session_name"] for row in result.fetchall()]


async def dequeue_messages(conn: AsyncConnection, session_name: str, limit: int = 10) -> list[dict]:
    """Atomically claim pending messages for a session, oldest first."""
    now = _now_iso()
    lock = "FOR UPDATE SKIP LOCKED" if conn.dialect.name == "postgresql" else ""
    sql = f"""UPDATE message_queue SET status='in_progress', leased_at=:now
             WHERE id IN (
                 SELECT id FROM message_queue
                 WHERE session_name=:session AND status='pending'
                 ORDER BY enqueued_at ASC LIMIT :lim
                 {lock}
             ) RETURNING *"""
    result = await conn.execute(text(sql), {"session": session_name, "lim": limit, "now": now})
    rows = [dict(row._mapping) for row in result.fetchall()]
    rows.sort(key=lambda row: row["enqueued_at"])
    return rows


async def release_lease(conn: AsyncConnection, message_id: int) -> None:
    await conn.execute(
        text(
            """UPDATE message_queue SET status='pending', leased_at=NULL
               WHERE id = :id AND status = 'in_progress'"""
        ),
        {"id": message_id},
    )


async def expire_stale_leases(conn: AsyncConnection, max_age_minutes: int = 5) -> int:
    cutoff = (datetime.now(UTC) - timedelta(minutes=max_age_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    result = await conn.execute(
        text(
            """UPDATE message_queue SET status='pending', leased_at=NULL
               WHERE status = 'in_progress' AND leased_at < :cutoff"""
        ),
        {"cutoff": cutoff},
    )
    return result.rowcount


async def mark_message_delivered(conn: AsyncConnection, message_id: int) -> None:
    await conn.execute(
        text(
            """UPDATE message_queue
               SET status = 'delivered', delivered_at = :delivered_at
               WHERE id = :id AND status = 'in_progress'"""
        ),
        {"delivered_at": _now_iso(), "id": message_id},
    )


async def expire_stale_pending(conn: AsyncConnection, max_age_minutes: int = 30) -> int:
    """Expire pending/leased messages older than the cutoff. Returns the count."""
    now = _now_iso()
    cutoff = (datetime.now(UTC) - timedelta(minutes=max_age_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    pending = await conn.execute(
        text(
            """UPDATE message_queue SET status = 'expired', delivered_at = :now
               WHERE status = 'pending' AND enqueued_at < :cutoff"""
        ),
        {"now": now, "cutoff": cutoff},
    )
    leased = await conn.execute(
        text(
            """UPDATE message_queue SET status='expired', delivered_at=:now
               WHERE status='in_progress' AND leased_at < :cutoff"""
        ),
        {"now": now, "cutoff": cutoff},
    )
    return (pending.rowcount or 0) + (leased.rowcount or 0)


async def purge_pending_for_issue(
    conn: AsyncConnection, issue_number: int, *, repo: str = ""
) -> int:
    """Mark pending/leased messages for an issue as delivered (issue closed)."""
    result = await conn.execute(
        text(
            """UPDATE message_queue
               SET status = 'delivered', delivered_at = :delivered_at
               WHERE repo = :repo AND issue_number = :issue_number
                 AND status IN ('pending', 'in_progress')"""
        ),
        {"delivered_at": _now_iso(), "repo": repo, "issue_number": issue_number},
    )
    return result.rowcount
