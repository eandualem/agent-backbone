"""Database configuration.

SQLite (file in the data directory) is the default and needs no setup.
Set ``url`` (or ``BACKBONE_DATABASE_URL``) to a ``postgresql+asyncpg://`` URL
to use PostgreSQL instead.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

SQLITE_FILENAME = "backbone.db"


class DatabaseConfig(BaseModel):
    """Connection settings. ``url`` empty means SQLite in the data directory."""

    url: str = ""
    pool_size: int = 5
    pool_overflow: int = 10
    echo: bool = False

    def resolved_url(self, data_dir: Path) -> str:
        """The async SQLAlchemy URL to connect with."""
        if self.url:
            return self.url
        return f"sqlite+aiosqlite:///{data_dir / SQLITE_FILENAME}"

    @staticmethod
    def is_sqlite(url: str) -> bool:
        return url.startswith("sqlite")

    @staticmethod
    def is_memory(url: str) -> bool:
        return ":memory:" in url
