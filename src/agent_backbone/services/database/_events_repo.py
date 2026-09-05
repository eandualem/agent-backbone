"""Inbound events — every webhook/poll event, before and after routing."""

from __future__ import annotations

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import cutoff_iso, now_iso


class EventRepo(Repo):
    async def poll_cursor(self, repo: str) -> str | None:
        """The persisted GitHub replay boundary, not an event receipt time."""
        async with self._tx() as conn:
            result = await conn.execute(
                text("SELECT since FROM poll_cursors WHERE repo = :repo"), {"repo": repo}
            )
            return result.scalar_one_or_none()

    async def save_poll_cursor(self, repo: str, since: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO poll_cursors (repo, since) VALUES (:repo, :since) "
                    "ON CONFLICT (repo) DO UPDATE SET since = excluded.since"
                ),
                {"repo": repo, "since": since},
            )

    async def record(
        self,
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
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    """INSERT INTO events
                       (delivery_id, source, repo, event_type, issue_number, sender,
                        summary, received_at)
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
                    "received_at": now_iso(),
                },
            )
            row = result.fetchone()
            if row is not None:
                return row._mapping["id"]
            # The delivery id is already stored. If that earlier attempt never finished
            # routing (no processed_at), hand back the existing row so the event is
            # replayed instead of silently dropped.
            existing = await conn.execute(
                text("SELECT id, processed_at FROM events WHERE delivery_id = :delivery_id"),
                {"delivery_id": delivery_id},
            )
            existing_row = existing.fetchone()
            if existing_row is not None and existing_row._mapping["processed_at"] is None:
                return existing_row._mapping["id"]
            return None

    async def mark_processed(self, event_id: int, outcome: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text("UPDATE events SET processed_at = :now, outcome = :outcome WHERE id = :id"),
                {"now": now_iso(), "outcome": outcome[:500], "id": event_id},
            )

    async def query(
        self,
        *,
        repo: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        async with self._tx() as conn:
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

    async def last_time_by_repo(self) -> dict[str, str]:
        """Most recent ``received_at`` per repository (for status)."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT repo, MAX(received_at) AS last FROM events"
                    " WHERE repo != '' GROUP BY repo"
                )
            )
            return {row._mapping["repo"]: row._mapping["last"] for row in result.fetchall()}

    async def prune(self, retention_days: int = 30) -> int:
        async with self._tx() as conn:
            result = await conn.execute(
                text("DELETE FROM events WHERE received_at < :cutoff"),
                {"cutoff": cutoff_iso(days=retention_days)},
            )
            return result.rowcount
