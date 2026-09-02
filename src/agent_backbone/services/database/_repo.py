"""Repository base: one class per table family, each holding the engine."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncEngine


class Repo:
    """Queries for one table family. ``_tx()`` opens a transaction on the live engine."""

    def __init__(self, engine: Callable[[], AsyncEngine]) -> None:
        self._engine = engine

    def _tx(self):
        return self._engine().begin()
