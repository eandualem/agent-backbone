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
