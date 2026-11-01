"""Repository base: one class per table family, each holding the engine."""

from __future__ import annotations

import json
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class Repo:
    """Queries for one table family. ``_tx()`` opens a transaction on the live engine."""

    def __init__(self, engine: Callable[[], AsyncEngine]) -> None:
        self._engine = engine

    def _tx(self):
        return self._engine().begin()


async def diagnostic_mode(conn) -> str:
    """Read the persisted capture switch inside the writer's transaction."""
    raw = await conn.scalar(text("SELECT value FROM settings WHERE key='diagnostics.mode'"))
    if raw is None:
        return "standard"
    try:
        mode = json.loads(raw)
    except (ValueError, TypeError):
        return "off"
    return mode if mode in ("off", "standard", "debug") else "off"


CAPTURE_GENERATION_SQL = (
    "COALESCE((SELECT recorder_id FROM interaction_capture_checkpoints "
    "WHERE reason='diagnostics_mode_changed' ORDER BY id DESC LIMIT 1),'initial')"
)


async def diagnostic_capture_policy(conn) -> tuple[str, str]:
    """Read policy and its durable boundary together, independent of notifications."""
    row = (
        await conn.execute(
            text(
                "SELECT (SELECT value FROM settings WHERE key='diagnostics.mode') AS value, "
                + CAPTURE_GENERATION_SQL
                + " AS generation"
            )
        )
    ).one()
    if row.value is None:
        mode = "standard"
    else:
        try:
            mode = json.loads(row.value)
        except (ValueError, TypeError):
            mode = "off"
    return (mode if mode in ("off", "standard", "debug") else "off", row.generation)
