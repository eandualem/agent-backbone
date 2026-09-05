"""Durable per-recipient delivery plans and receipts for GitHub events."""

from __future__ import annotations

import json

from sqlalchemy import text

from agent_backbone.services.database._repo import Repo
from agent_backbone.services.database._time import now_iso


class OutboxRepo(Repo):
    async def plan(self, event_id: int, deliveries: list[dict]) -> None:
        """Persist the entire audience atomically before the first delivery."""
        async with self._tx() as conn:
            for delivery in deliveries:
                await conn.execute(
                    text(
                        "INSERT INTO event_outbox "
                        "(event_id, recipient, delivery, status, updated_at) "
                        "VALUES (:event_id, :recipient, :delivery, 'pending', :now) "
                        "ON CONFLICT (event_id, recipient) DO NOTHING"
                    ),
                    {
                        "event_id": event_id,
                        "recipient": delivery["session_name"],
                        "delivery": json.dumps(delivery),
                        "now": now_iso(),
                    },
                )

    async def entries(self, event_id: int) -> list[dict]:
        async with self._tx() as conn:
            result = await conn.execute(
                text("SELECT * FROM event_outbox WHERE event_id = :id ORDER BY recipient"),
                {"id": event_id},
            )
            return [
                {**dict(row._mapping), "delivery": json.loads(row._mapping["delivery"])}
                for row in result.fetchall()
            ]

    async def set_status(self, event_id: int, recipient: str, status: str) -> None:
        if status not in {"failed", "delivered", "queued", "skipped"}:
            raise ValueError(f"invalid outbox status: {status}")
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE event_outbox SET status = :status, updated_at = :now "
                    "WHERE event_id = :id AND recipient = :recipient "
                    "AND status IN ('pending', 'failed')"
                ),
                {"id": event_id, "recipient": recipient, "status": status, "now": now_iso()},
            )

    async def pending_events(self, limit: int = 20) -> list[int]:
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "SELECT o.event_id FROM event_outbox o JOIN events e ON e.id = o.event_id "
                    "WHERE e.processed_at IS NULL "
                    "GROUP BY o.event_id ORDER BY MIN(o.updated_at), o.event_id LIMIT :limit"
                ),
                {"limit": limit},
            )
            return list(result.scalars())

    async def finish_event(self, event_id: int, outcome: str) -> bool:
        """Mark handled only when every recipient has a terminal receipt."""
        async with self._tx() as conn:
            result = await conn.execute(
                text(
                    "UPDATE events SET processed_at = :now, outcome = :outcome "
                    "WHERE id = :id AND NOT EXISTS (SELECT 1 FROM event_outbox "
                    "WHERE event_id = :id AND status IN ('pending', 'failed'))"
                ),
                {"id": event_id, "now": now_iso(), "outcome": outcome[:500]},
            )
            return bool(result.rowcount)

    async def discard_issue(self, repo: str, issue_number: int) -> None:
        """Retire pending notifications when their issue or PR closes."""
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "UPDATE event_outbox SET status = 'skipped', updated_at = :now "
                    "WHERE status IN ('pending', 'failed') AND event_id IN "
                    "(SELECT id FROM events WHERE repo = :repo AND issue_number = :issue)"
                ),
                {"repo": repo, "issue": issue_number, "now": now_iso()},
            )
