"""Helpers shared by the test suite (not fixtures)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

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


def opencode_db(path: Path, messages: list[tuple[str, dict, list[dict]]]) -> Path:
    """An OpenCode database with the ``message`` and ``part`` tables it keeps
    (1.18), holding ``(session id, message data, [part data, ...])`` in order,
    so part rowids follow that order."""
    conn = sqlite3.connect(path)
    with conn:
        for table, parent in (("message", ""), ("part", "message_id TEXT NOT NULL, ")):
            conn.execute(
                f"CREATE TABLE {table} (id TEXT PRIMARY KEY, {parent}session_id TEXT NOT NULL, "
                "time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL)"
            )
        for m, (session, data, parts) in enumerate(messages):
            conn.execute(
                "INSERT INTO message VALUES (?,?,0,0,?)", (f"msg_{m}", session, json.dumps(data))
            )
            for n, part in enumerate(parts):
                conn.execute(
                    "INSERT INTO part VALUES (?,?,?,0,0,?)",
                    (f"prt_{m}_{n}", f"msg_{m}", session, json.dumps(part)),
                )
    conn.close()
    return path
