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


def opencode_session_table(conn):
    """Current native session fields used for discovery and usage reconciliation."""
    conn.execute(
        "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, parent_id TEXT, cost REAL DEFAULT 0, "
        "tokens_input INTEGER DEFAULT 0, tokens_output INTEGER DEFAULT 0, "
        "tokens_reasoning INTEGER DEFAULT 0, tokens_cache_read INTEGER DEFAULT 0, "
        "tokens_cache_write INTEGER DEFAULT 0, fork_session_id TEXT, "
        "time_created INTEGER DEFAULT 0, time_updated INTEGER DEFAULT 0)"
    )


def opencode_db(path: Path, messages: list[tuple[str, dict, list[dict]]]) -> Path:
    """Current native session/message tables, with ordered message content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        opencode_session_table(conn)
        conn.execute(
            "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
            "seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        for index, (session, data, parts) in enumerate(messages):
            conn.execute(
                "INSERT OR IGNORE INTO session_v2(id,parent_id) VALUES (?,NULL)", (session,)
            )
            created = 1790071200001 + index
            completed = max((p.get("time", {}).get("end", 0) for p in parts), default=0)
            native = {"content": parts, "time": {"created": created}}
            if completed:
                native["time"]["completed"] = completed
            kind = "compaction" if data.get("summary") else data["role"]
            conn.execute(
                "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                (f"msg_{index}", session, kind, index, created, created, json.dumps(native)),
            )
    return path


def gemini_session(path: Path, session_id: str, records: list[dict | str]) -> Path:
    """A Gemini CLI session file (0.46): the header line with the session id,
    then each record as the compact JSON line Gemini appends (a string is
    written as it is, for a malformed line)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"sessionId": session_id, "projectHash": "0" * 64, "kind": "main"}
    lines = [r if isinstance(r, str) else json.dumps(r, separators=(",", ":")) for r in records]
    path.write_text("\n".join([json.dumps(header, separators=(",", ":")), *lines]) + "\n")
    return path


async def register_config_agents(db, config):
    """Routing fixtures model the database registrations behind their snapshot."""
    fields = (
        "dir",
        "runtime",
        "model",
        "repo",
        "tags",
        "env",
        "description",
        "always_on",
        "unattended",
        "inbox_only",
    )
    for spec in config.agents:
        await db.agents.upsert(spec.name, **{name: getattr(spec, name) for name in fields})


async def registration_id(db, name):
    return next(row["report_identity"] for row in await db.agents.list() if row["name"] == name)
