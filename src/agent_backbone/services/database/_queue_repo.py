"""The message queue — deferred deliveries, leased in batches."""

from __future__ import annotations

import hashlib

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, now_iso

_INSERT_COLUMNS = """(session_name, message, repo, issue_number, target_entity,
                delivery_kind, source, enqueued_at, status, content_hash)
               VALUES (:session_name, :message, :repo, :issue_number, :target_entity,
                       :delivery_kind, :source, :enqueued_at, 'pending', :content_hash)"""


class QueueRepo(Repo):
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
    ) -> int:
        """Enqueue a message for later delivery. Returns the row ID or -1 when deduped."""
        async with self._tx() as conn:
            content_hash = hashlib.sha256(message.encode()).hexdigest()
            params = {
                "session_name": session_name,
                "message": message,
                "repo": repo,
                "issue_number": issue_number,
                "target_entity": target_entity,
                "delivery_kind": delivery_kind,
                "source": source,
                "enqueued_at": now_iso(),
                "content_hash": content_hash,
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
                conflict = """ON CONFLICT (session_name, content_hash)
                       WHERE delivery_kind != 'issue' AND status IN ('pending','in_progress')
                       DO NOTHING"""

            sql = f"INSERT INTO message_queue {_INSERT_COLUMNS} {conflict} RETURNING id"
            result = await conn.execute(text(sql), params)
            row = result.fetchone()
            return row._mapping["id"] if row else -1

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

    async def mark_delivered(self, message_id: int) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    """UPDATE message_queue
                       SET status = 'delivered', delivered_at = :delivered_at
                       WHERE id = :id AND status = 'in_progress'"""
                ),
                {"delivered_at": now_iso(), "id": message_id},
            )

    async def expire_pending(self, max_age_minutes: int = 30) -> int:
        """Expire pending messages older than the cutoff. Returns the count.

        Leased rows are not considered: ``expire_stale_leases`` returns them to
        ``pending`` long before this cutoff, so they expire on the next sweep.
        """
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """UPDATE message_queue SET status = 'expired', delivered_at = :now
                       WHERE status = 'pending' AND enqueued_at < :cutoff"""
                ),
                {"now": now_iso(), "cutoff": cutoff_iso(minutes=max_age_minutes)},
            )
            return result.rowcount or 0

    async def purge_for_issue(self, issue_number: int, *, repo: str = "") -> int:
        """Mark pending/leased messages for an issue as delivered (issue closed)."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """UPDATE message_queue
                       SET status = 'delivered', delivered_at = :delivered_at
                       WHERE repo = :repo AND issue_number = :issue_number
                         AND status IN ('pending', 'in_progress')"""
                ),
                {"delivered_at": now_iso(), "repo": repo, "issue_number": issue_number},
            )
            return result.rowcount
