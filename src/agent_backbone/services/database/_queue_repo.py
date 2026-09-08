"""The message queue — deferred deliveries, leased in batches."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import text

from agent_backbone.services.database._diagnostics_repo import DiagnosticRepo
from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, now_iso

_INSERT_COLUMNS = """(operation_id, session_name, message, repo, issue_number, target_entity,
                delivery_kind, source, enqueued_at, status, sender, dedup_key)
               VALUES (:operation_id, :session_name, :message, :repo, :issue_number, :target_entity,
                       :delivery_kind, :source, :enqueued_at, 'pending', :sender, :dedup_key)"""


@dataclass(frozen=True)
class EnqueueResult:
    """What ``enqueue`` did: ``inserted`` (a new row) or
    ``already_queued`` (the same message is already waiting — nothing added).
    Both return the stored row's id and operation identity.
    A database error is raised, never swallowed: the caller decides what to
    tell the sender."""

    status: str
    id: int | None = None
    operation_id: str | None = None

    @property
    def stored(self) -> bool:
        """Whether a row for this message now exists in the queue."""
        return self.status in ("inserted", "already_queued")


def dedup_key_for(message: str, sender: str, source_key: str | None) -> str:
    """The identity of a non-issue message for duplicate detection.

    The source event (a GitHub comment id, an issue lifecycle event) when
    the caller has one; otherwise the sender and the text together, so the
    same words from two senders are two messages and one sender repeating
    itself by accident is one.
    """
    if source_key:
        return f"src:{source_key}"
    digest = hashlib.sha256(f"{sender}\x00{message}".encode()).hexdigest()
    return f"msg:{digest}"


class QueueRepo(Repo):
    async def prune(self, retention_days: int = 30) -> int:
        """Delete completed queue bodies after retention, measured from completion.

        Pending and leased messages are never deleted; expiry is a separate
        operation that records their outcome. Legacy rows without a completion
        timestamp are retained because their age cannot be established.
        """
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "DELETE FROM message_queue WHERE status IN ('delivered', 'expired')"
                    " AND delivered_at < :cutoff"
                ),
                {"cutoff": cutoff_iso(days=retention_days)},
            )
            return result.rowcount

    async def enqueue(
        self,
        *,
        session_name: str,
        message: str,
        issue_number: int | None = None,
        target_entity: str | None = None,
        delivery_kind: str = "issue",
        source: str = "",
        repo: str = "",
        sender: str = "",
        source_key: str | None = None,
        operation_id: str | None = None,
    ) -> EnqueueResult:
        """Store a message for later delivery.

        ``sender`` and ``source_key`` form the duplicate key for non-issue
        kinds (``dedup_key_for``); issue notifications are unique per
        ``(session, repo, issue)`` while pending.
        """
        async with self._tx() as conn:
            params = {
                "operation_id": operation_id or uuid.uuid4().hex,
                "session_name": session_name,
                "message": message,
                "repo": repo,
                "issue_number": issue_number,
                "target_entity": target_entity,
                "delivery_kind": delivery_kind,
                "source": source,
                "enqueued_at": now_iso(),
                "sender": sender,
                "dedup_key": dedup_key_for(message, sender, source_key),
            }

            if delivery_kind == "issue" and issue_number is not None:
                conflict = """ON CONFLICT (session_name, repo, issue_number)
                       WHERE delivery_kind = 'issue'
                         AND status IN ('pending','in_progress')
                         AND issue_number IS NOT NULL
                       DO NOTHING"""
            elif delivery_kind == "issue":
                conflict = ""
            else:
                conflict = """ON CONFLICT (session_name, dedup_key)
                       WHERE delivery_kind != 'issue' AND status IN ('pending','in_progress')
                       DO NOTHING"""

            sql = f"INSERT INTO message_queue {_INSERT_COLUMNS} {conflict} RETURNING id"
            result = await conn.execute(text(sql), params)
            row = result.fetchone()
            if row is None:
                # Resolve the exact conflict key, including a row leased by a
                # drain. Never correlate using a message preview or its age.
                key = (
                    "session_name = :session_name AND repo = :repo "
                    "AND issue_number = :issue_number AND delivery_kind = 'issue'"
                    if delivery_kind == "issue"
                    else "session_name = :session_name AND dedup_key = :dedup_key "
                    "AND delivery_kind != 'issue'"
                )
                existing = await conn.execute(
                    text(
                        "UPDATE message_queue SET operation_id = "
                        "COALESCE(operation_id, :operation_id) "
                        f"WHERE {key} AND status IN ('pending', 'in_progress') "
                        "RETURNING id, operation_id"
                    ),
                    params,
                )
                stored = existing.mappings().one()
                return EnqueueResult("already_queued", stored["id"], stored["operation_id"])
            return EnqueueResult("inserted", row._mapping["id"], params["operation_id"])

    async def pending_count(self, session_name: str) -> int:
        """How many messages are waiting for one session."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT COUNT(*) FROM message_queue "
                    "WHERE session_name = :session AND status = 'pending'"
                ),
                {"session": session_name},
            )
            return int(result.scalar_one())

    async def sessions_with_pending(self) -> list[str]:
        async with self._tx() as conn:
            result = await conn.execute(
                text("SELECT DISTINCT session_name FROM message_queue WHERE status = 'pending'")
            )
            return [row._mapping["session_name"] for row in result.fetchall()]

    async def dequeue(self, session_name: str, limit: int = 10) -> list[dict]:
        """Atomically claim pending messages for a session, oldest first."""
        async with self._tx() as conn:
            now = now_iso()
            lock = "FOR UPDATE SKIP LOCKED" if conn.dialect.name == "postgresql" else ""
            sql = f"""UPDATE message_queue SET status='in_progress', leased_at=:now
                     WHERE id IN (
                         SELECT id FROM message_queue
                         WHERE session_name=:session AND status='pending'
                         ORDER BY enqueued_at ASC LIMIT :lim
                         {lock}
                     ) RETURNING *"""
            result = await conn.execute(
                text(sql), {"session": session_name, "lim": limit, "now": now}
            )
            rows = [dict(row._mapping) for row in result.fetchall()]
            for row in rows:
                await self._ensure_operation_id(conn, row)
            rows.sort(key=lambda row: row["enqueued_at"])
            return rows

    async def release(self, message_id: int) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """UPDATE message_queue SET status='pending', leased_at=NULL
                       WHERE id = :id AND status = 'in_progress'"""
                ),
                {"id": message_id},
            )

    async def expire_stale_leases(self, max_age_minutes: int = 5) -> int:
        """Return leases older than the cutoff to ``pending`` (the deliverer died mid-batch)."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """UPDATE message_queue SET status='pending', leased_at=NULL
                       WHERE status = 'in_progress' AND leased_at < :cutoff"""
                ),
                {"cutoff": cutoff_iso(minutes=max_age_minutes)},
            )
            return result.rowcount

    async def mark_delivered(self, message_id: int, *, reason: str | None = None) -> None:
        """Complete the lease; ``reason`` identifies intentional retirement."""
        if reason is not None and reason not in {
            "acknowledged",
            "no_repo",
            "issue_closed",
            "no_longer_targeted",
            "already_delivered",
        }:
            raise ValueError("Invalid queue retirement reason")
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """UPDATE message_queue
                       SET status = 'delivered', delivered_at = :delivered_at
                       WHERE id = :id AND status = 'in_progress' RETURNING *"""
                ),
                {"delivered_at": now_iso(), "id": message_id},
            )
            row = result.mappings().first()
            if row is not None:
                row = dict(row)
                await self._ensure_operation_id(conn, row)
        if row is not None and reason is not None:
            await self._record_lifecycle(row, f"retired_{reason}", reason=reason)

    async def _ensure_operation_id(self, conn, row: dict) -> None:
        """Give legacy rows one identity, derived only from their exact database key."""
        if not row.get("operation_id"):
            row["operation_id"] = uuid.uuid5(uuid.NAMESPACE_URL, f"backbone:queue:{row['id']}").hex
            await conn.execute(
                text("UPDATE message_queue SET operation_id = :operation WHERE id = :id"),
                {"operation": row["operation_id"], "id": row["id"]},
            )

    async def _record_lifecycle(
        self, row, code: str, *, reason: str | None = None, delivery_id: int | None = None
    ) -> None:
        """Persist metadata after completion; diagnostics cannot roll back the queue."""
        await DiagnosticRepo(self._engine).record(
            category="queue",
            code=code,
            operation_id=row["operation_id"]
            or uuid.uuid5(uuid.NAMESPACE_URL, f"backbone:queue:{row['id']}").hex,
            severity="warning" if code == "queue_expired" else "info",
            agent_name=row["session_name"],
            source=row["source"],
            repo=row["repo"],
            issue_number=row["issue_number"],
            queue_id=row["id"],
            delivery_id=delivery_id,
            details={
                "delivery_kind": row["delivery_kind"],
                "queue_status": row["status"],
                "reason": reason,
            },
        )

    async def expire_pending(self, max_age_minutes: int = 30) -> list[dict]:
        """Expire pending messages older than the cutoff and, in the same
        transaction, leave a delivery row with outcome ``expired`` for each,
        so a dropped message is never lost from the record. Returns the rows.

        Leased rows are not considered: ``expire_stale_leases`` returns them to
        ``pending`` long before this cutoff, so they expire on the next sweep.
        """
        async with self._tx() as conn:
            now = now_iso()
            result = await conn.execute(
                text(
                    """UPDATE message_queue SET status = 'expired', delivered_at = :now
                       WHERE status = 'pending' AND enqueued_at < :cutoff
                       RETURNING *"""
                ),
                {"now": now, "cutoff": cutoff_iso(minutes=max_age_minutes)},
            )
            rows = [dict(row._mapping) for row in result.fetchall()]
            for row in rows:
                await self._ensure_operation_id(conn, row)
                message = row.get("message") or ""
                delivery = await conn.execute(
                    text(
                        """INSERT INTO deliveries
                           (operation_id, kind, repo, issue_number, target_entity, session_name,
                            outcome, source, preview, created_at)
                           VALUES (:operation_id, :kind, :repo, :issue_number, :target_entity,
                                   :session_name, 'expired', :source, :preview, :created_at)
                           RETURNING id"""
                    ),
                    {
                        "operation_id": row["operation_id"],
                        "kind": row.get("delivery_kind") or "issue",
                        "repo": row.get("repo") or "",
                        "issue_number": row.get("issue_number"),
                        "target_entity": row.get("target_entity") or row.get("session_name") or "",
                        "session_name": row.get("session_name") or "",
                        "source": row.get("source") or "queue",
                        "preview": message[:120],
                        "created_at": now,
                    },
                )
                row["delivery_id"] = delivery.scalar_one()
        for row in rows:
            await self._record_lifecycle(row, "queue_expired", delivery_id=row["delivery_id"])
        return rows

    async def purge_for_issue(self, issue_number: int, *, repo: str = "") -> int:
        """Mark pending/leased messages for an issue as delivered (issue closed)."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """UPDATE message_queue
                       SET status = 'delivered', delivered_at = :delivered_at
                       WHERE repo = :repo AND issue_number = :issue_number
                         AND status IN ('pending', 'in_progress') RETURNING *"""
                ),
                {"delivered_at": now_iso(), "repo": repo, "issue_number": issue_number},
            )
            rows = [dict(row) for row in result.mappings()]
            for row in rows:
                await self._ensure_operation_id(conn, row)
        for row in rows:
            await self._record_lifecycle(row, "retired_issue_closed", reason="issue_closed")
        return len(rows)
