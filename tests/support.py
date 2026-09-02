"""Helpers shared by the test suite (not fixtures)."""

from __future__ import annotations

from sqlalchemy import text

from agent_backbone.services.database import BackboneDB


async def queue_row(db: BackboneDB, message_id: int) -> dict | None:
    """One ``message_queue`` row by id — the tests' window into the queue."""
    async with db.engine.begin() as conn:
        result = await conn.execute(
            text("SELECT * FROM message_queue WHERE id = :id"), {"id": message_id}
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None
