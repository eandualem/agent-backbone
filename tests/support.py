"""Helpers shared by the test suite (not fixtures)."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from agent_backbone.api import session_updates
from agent_backbone.services.database import BackboneDB


def reset_session_updates() -> None:
    """Forget the cached session snapshot and the last broadcast signature."""
    session_updates._snapshot_cache_lock = asyncio.Lock()
    session_updates._sessions_update_lock = asyncio.Lock()
    session_updates._snapshot_cache = []
    session_updates._snapshot_cache_ts = 0.0
    session_updates._last_sessions_update_signature = None


async def queue_row(db: BackboneDB, message_id: int) -> dict | None:
    """One ``message_queue`` row by id — the tests' window into the queue."""
    async with db._engine.begin() as conn:
        result = await conn.execute(
            text("SELECT * FROM message_queue WHERE id = :id"), {"id": message_id}
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None
