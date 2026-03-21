"""Queue, dedup, dependencies, acknowledgments, and heartbeat repository."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, distinct, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from agent_backbone.models import normalize_repo_full_name
from agent_backbone.services.database.models import (
    AcknowledgmentORM,
    DedupLogORM,
    HeartbeatORM,
    IssueDependencyORM,
    MessageQueueORM,
)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dialect_insert(conn: AsyncConnection, model):
    if conn.dialect.name == "postgresql":
        return pg_insert(model)
    if conn.dialect.name == "sqlite":
        return sqlite_insert(model)
    raise RuntimeError(f"Unsupported dialect for conflict-aware insert: {conn.dialect.name}")


def _rows_to_dicts(result) -> list[dict]:
    return [dict(row._mapping) for row in result.fetchall()]


# --- Dedup ---


async def is_duplicate_delivery_id(
    conn: AsyncConnection,
    delivery_id: str,
) -> bool:
    """Check if a delivery ID has been seen before."""
    if not delivery_id:
        return False
    result = await conn.execute(
        select(DedupLogORM.delivery_id).where(DedupLogORM.delivery_id == delivery_id).limit(1)
    )
    return result.fetchone() is not None


async def record_delivery_id(
    conn: AsyncConnection,
    delivery_id: str,
) -> None:
    """Record a delivery ID for dedup."""
    if not delivery_id:
        return
    stmt = (
        _dialect_insert(conn, DedupLogORM)
        .values(delivery_id=delivery_id, received_at=_now_iso())
        .on_conflict_do_nothing(index_elements=[DedupLogORM.delivery_id])
    )
    await conn.execute(stmt)


async def prune_delivery_ids(
    conn: AsyncConnection,
    max_age_hours: int = 24,
) -> int:
    """Remove old dedup entries. Returns count deleted."""
    cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
    result = await conn.execute(delete(DedupLogORM).where(DedupLogORM.received_at < cutoff))
    return result.rowcount


# --- Issue dependencies ---


async def upsert_dependency(
    conn: AsyncConnection,
    parent: int,
    sub: int,
) -> None:
    """Record a parent->sub-issue dependency."""
    stmt = (
        _dialect_insert(conn, IssueDependencyORM)
        .values(parent_number=parent, sub_issue_number=sub, updated_at=_now_iso())
        .on_conflict_do_update(
            index_elements=[
                IssueDependencyORM.parent_number,
                IssueDependencyORM.sub_issue_number,
            ],
            set_={"updated_at": _now_iso()},
        )
    )
    await conn.execute(stmt)


async def get_parents(
    conn: AsyncConnection,
    sub_issue_number: int,
) -> list[int]:
    """Get parent issue numbers for a given sub-issue."""
    result = await conn.execute(
        select(IssueDependencyORM.parent_number).where(
            IssueDependencyORM.sub_issue_number == sub_issue_number
        )
    )
    return [row._mapping["parent_number"] for row in result.fetchall()]


async def sync_dependencies(
    conn: AsyncConnection,
    parent: int,
    sub_issues: list[int],
) -> None:
    """Sync the dependency table for a parent."""
    now = _now_iso()
    if sub_issues:
        upsert_stmt = (
            _dialect_insert(conn, IssueDependencyORM)
            .values(
                parent_number=parent,
                sub_issue_number=None,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[
                    IssueDependencyORM.parent_number,
                    IssueDependencyORM.sub_issue_number,
                ],
                set_={"updated_at": now},
            )
        )
        for sub in sub_issues:
            await conn.execute(
                upsert_stmt.values(
                    parent_number=parent,
                    sub_issue_number=sub,
                    updated_at=now,
                )
            )
        await conn.execute(
            delete(IssueDependencyORM).where(
                IssueDependencyORM.parent_number == parent,
                IssueDependencyORM.sub_issue_number.not_in(sub_issues),
            )
        )
        return

    await conn.execute(delete(IssueDependencyORM).where(IssueDependencyORM.parent_number == parent))


# --- Acknowledgments ---


async def record_acknowledgment(
    conn: AsyncConnection,
    repo_full_name: str | None,
    issue_number: int,
    target_entity: str,
) -> None:
    """Record that an entity has acknowledged an issue."""
    stmt = (
        _dialect_insert(conn, AcknowledgmentORM)
        .values(
            repo_full_name=normalize_repo_full_name(repo_full_name),
            issue_number=issue_number,
            target_entity=target_entity,
            acknowledged_at=_now_iso(),
        )
        .on_conflict_do_update(
            index_elements=[
                AcknowledgmentORM.repo_full_name,
                AcknowledgmentORM.issue_number,
                AcknowledgmentORM.target_entity,
            ],
            set_={"acknowledged_at": _now_iso()},
        )
    )
    await conn.execute(stmt)


async def is_acknowledged(
    conn: AsyncConnection,
    repo_full_name: str | None,
    issue_number: int,
    target_entity: str,
) -> bool:
    """Check if entity has acknowledged this issue."""
    result = await conn.execute(
        select(AcknowledgmentORM.issue_number)
        .where(
            AcknowledgmentORM.repo_full_name == normalize_repo_full_name(repo_full_name),
            AcknowledgmentORM.issue_number == issue_number,
            AcknowledgmentORM.target_entity == target_entity,
        )
        .limit(1)
    )
    return result.fetchone() is not None


async def clear_acknowledgment(
    conn: AsyncConnection,
    repo_full_name: str | None,
    issue_number: int,
    target_entity: str,
) -> None:
    """Clear acknowledgment for entity on issue."""
    await conn.execute(
        delete(AcknowledgmentORM).where(
            AcknowledgmentORM.repo_full_name == normalize_repo_full_name(repo_full_name),
            AcknowledgmentORM.issue_number == issue_number,
            AcknowledgmentORM.target_entity == target_entity,
        )
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
        (
            pg_insert(HeartbeatORM)
            if conn.dialect.name == "postgresql"
            else sqlite_insert(HeartbeatORM)
        )
        .values(
            agent=agent,
            delivered_at=_now_iso(),
            outcome=outcome,
            message=message,
        )
        .returning(HeartbeatORM.id)
    )
    return result.scalar_one()


async def get_last_heartbeat(
    conn: AsyncConnection,
    agent: str,
    outcome: str = "delivered",
) -> str | None:
    """Get the delivered_at ISO string of the most recent matching heartbeat."""
    result = await conn.execute(
        select(HeartbeatORM.delivered_at)
        .where(HeartbeatORM.agent == agent, HeartbeatORM.outcome == outcome)
        .order_by(HeartbeatORM.delivered_at.desc())
        .limit(1)
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
    stmt = select(HeartbeatORM.__table__)
    if agent is not None:
        stmt = stmt.where(HeartbeatORM.agent == agent)
    if outcome is not None:
        stmt = stmt.where(HeartbeatORM.outcome == outcome)
    result = await conn.execute(stmt.order_by(HeartbeatORM.delivered_at.desc()).limit(limit))
    return _rows_to_dicts(result)


# --- Message queue ---


async def enqueue_message(
    conn: AsyncConnection,
    session_name: str,
    message: str,
    repo_full_name: str | None = None,
    issue_number: int | None = None,
    target_entity: str | None = None,
    delivery_kind: str = "issue",
    flow_name: str = "",
) -> int:
    """Enqueue a message for later delivery. Returns the row ID or -1 when deduped."""
    content_hash = hashlib.sha256(message.encode()).hexdigest()
    stmt = _dialect_insert(conn, MessageQueueORM).values(
        session_name=session_name,
        message=message,
        repo_full_name=normalize_repo_full_name(repo_full_name),
        issue_number=issue_number,
        target_entity=target_entity,
        delivery_kind=delivery_kind,
        flow_name=flow_name,
        enqueued_at=_now_iso(),
        status="pending",
        content_hash=content_hash,
    )

    if delivery_kind == "issue" and issue_number is not None:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[
                MessageQueueORM.session_name,
                MessageQueueORM.repo_full_name,
                MessageQueueORM.issue_number,
            ],
            index_where=and_(
                MessageQueueORM.delivery_kind == "issue",
                MessageQueueORM.status.in_(("pending", "in_progress")),
                MessageQueueORM.issue_number.is_not(None),
            ),
        )
    elif delivery_kind == "comment" and issue_number is not None:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[
                MessageQueueORM.session_name,
                MessageQueueORM.repo_full_name,
                MessageQueueORM.issue_number,
                MessageQueueORM.content_hash,
            ],
            index_where=and_(
                MessageQueueORM.delivery_kind == "comment",
                MessageQueueORM.status.in_(("pending", "in_progress")),
                MessageQueueORM.issue_number.is_not(None),
            ),
        )
    elif delivery_kind == "direct_message":
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[MessageQueueORM.session_name, MessageQueueORM.content_hash],
            index_where=and_(
                MessageQueueORM.delivery_kind == "direct_message",
                MessageQueueORM.status.in_(("pending", "in_progress")),
            ),
        )

    result = await conn.execute(stmt.returning(MessageQueueORM.id))
    row = result.fetchone()
    return row._mapping["id"] if row else -1


async def get_sessions_with_pending(conn: AsyncConnection) -> list[str]:
    """List sessions that currently have pending queue rows."""
    result = await conn.execute(
        select(distinct(MessageQueueORM.session_name)).where(MessageQueueORM.status == "pending")
    )
    return [row._mapping["session_name"] for row in result.fetchall()]


async def dequeue_messages(
    conn: AsyncConnection,
    session_name: str,
    limit: int = 10,
) -> list[dict]:
    """Atomically claim pending messages for a session, oldest first."""
    now = _now_iso()
    pending_ids = (
        select(MessageQueueORM.id)
        .where(
            MessageQueueORM.session_name == session_name,
            MessageQueueORM.status == "pending",
        )
        .order_by(MessageQueueORM.enqueued_at.asc())
        .limit(limit)
    )
    if conn.dialect.name == "postgresql":
        pending_ids = pending_ids.with_for_update(skip_locked=True)

    result = await conn.execute(
        update(MessageQueueORM)
        .where(MessageQueueORM.id.in_(pending_ids))
        .values(status="in_progress", leased_at=now)
        .returning(*MessageQueueORM.__table__.c)
    )
    rows = _rows_to_dicts(result)
    rows.sort(key=lambda row: row["enqueued_at"])
    return rows


async def release_lease(
    conn: AsyncConnection,
    message_id: int,
) -> None:
    """Return an in-progress queue row to pending."""
    await conn.execute(
        update(MessageQueueORM)
        .where(MessageQueueORM.id == message_id, MessageQueueORM.status == "in_progress")
        .values(status="pending", leased_at=None)
    )


async def expire_stale_leases(
    conn: AsyncConnection,
    max_age_minutes: int = 5,
) -> int:
    """Return abandoned in-progress rows to pending."""
    cutoff = (datetime.now(UTC) - timedelta(minutes=max_age_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    result = await conn.execute(
        update(MessageQueueORM)
        .where(
            MessageQueueORM.status == "in_progress",
            MessageQueueORM.leased_at < cutoff,
        )
        .values(status="pending", leased_at=None)
    )
    return result.rowcount


async def mark_message_delivered(
    conn: AsyncConnection,
    message_id: int,
) -> None:
    """Mark a claimed queued message as delivered."""
    await conn.execute(
        update(MessageQueueORM)
        .where(MessageQueueORM.id == message_id, MessageQueueORM.status == "in_progress")
        .values(status="delivered", delivered_at=_now_iso())
    )


async def mark_matching_messages_delivered(
    conn: AsyncConnection,
    *,
    session_name: str,
    message: str,
    delivery_kind: str,
    repo_full_name: str | None = None,
    issue_number: int | None = None,
) -> int:
    """Mark queued messages with the same identity as delivered."""
    content_hash = hashlib.sha256(message.encode()).hexdigest()
    conditions = [
        MessageQueueORM.session_name == session_name,
        MessageQueueORM.delivery_kind == delivery_kind,
        MessageQueueORM.content_hash == content_hash,
        MessageQueueORM.status.in_(("pending", "in_progress")),
    ]
    conditions.append(MessageQueueORM.repo_full_name == normalize_repo_full_name(repo_full_name))
    if issue_number is None:
        conditions.append(MessageQueueORM.issue_number.is_(None))
    else:
        conditions.append(MessageQueueORM.issue_number == issue_number)

    result = await conn.execute(
        update(MessageQueueORM)
        .where(*conditions)
        .values(status="delivered", delivered_at=_now_iso(), leased_at=None)
    )
    return result.rowcount or 0


async def expire_stale_pending(
    conn: AsyncConnection,
    max_age_minutes: int = 30,
) -> int:
    """Mark stale pending and in-progress messages as expired."""
    now = _now_iso()
    cutoff_str = (datetime.now(UTC) - timedelta(minutes=max_age_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    pending_result = await conn.execute(
        update(MessageQueueORM)
        .where(
            MessageQueueORM.status == "pending",
            MessageQueueORM.enqueued_at < cutoff_str,
            MessageQueueORM.delivery_kind != "direct_message",
        )
        .values(status="expired", delivered_at=now)
    )
    in_progress_result = await conn.execute(
        update(MessageQueueORM)
        .where(
            MessageQueueORM.status == "in_progress",
            MessageQueueORM.leased_at < cutoff_str,
            MessageQueueORM.delivery_kind != "direct_message",
        )
        .values(status="expired", delivered_at=now)
    )
    dm_cutoff = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    dm_result = await conn.execute(
        update(MessageQueueORM)
        .where(
            MessageQueueORM.status.in_(("pending", "in_progress")),
            MessageQueueORM.delivery_kind == "direct_message",
            MessageQueueORM.enqueued_at < dm_cutoff,
        )
        .values(status="expired", delivered_at=now)
    )
    return (
        (pending_result.rowcount or 0)
        + (in_progress_result.rowcount or 0)
        + (dm_result.rowcount or 0)
    )


async def purge_pending_for_issue(
    conn: AsyncConnection,
    repo_full_name: str | None,
    issue_number: int,
) -> int:
    """Mark all pending or leased messages for an issue as delivered (issue closed)."""
    result = await conn.execute(
        update(MessageQueueORM)
        .where(
            MessageQueueORM.repo_full_name == normalize_repo_full_name(repo_full_name),
            MessageQueueORM.issue_number == issue_number,
            MessageQueueORM.status.in_(("pending", "in_progress")),
        )
        .values(status="delivered", delivered_at=_now_iso())
    )
    return result.rowcount
