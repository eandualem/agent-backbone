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
    """An OpenCode database with the ``session``, ``message`` and ``part``
    tables it keeps (1.18), holding ``(session id, message data, [part data,
    ...])`` in order. Each part is created a millisecond after the previous
    one unless its data carries ``_created`` (milliseconds)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
        for table, parent in (("message", ""), ("part", "message_id TEXT NOT NULL, ")):
            conn.execute(
                f"CREATE TABLE {table} (id TEXT PRIMARY KEY, {parent}session_id TEXT NOT NULL, "
                "time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL)"
            )
        created = 1790071200000
        for m, (session, data, parts) in enumerate(messages):
            conn.execute("INSERT OR IGNORE INTO session VALUES (?)", (session,))
            conn.execute(
                "INSERT INTO message VALUES (?,?,0,0,?)", (f"msg_{m}", session, json.dumps(data))
            )
            for n, part in enumerate(parts):
                part = dict(part)
                created = part.pop("_created", created + 1)
                conn.execute(
                    "INSERT INTO part VALUES (?,?,?,?,0,?)",
                    (f"prt_{m}_{n}", f"msg_{m}", session, created, json.dumps(part)),
                )
    conn.close()
    return path
