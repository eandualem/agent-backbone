"""Engine construction.

SQLite (a file in the data directory) is the default and needs no setup.
``BACKBONE_DATABASE_URL`` can point at PostgreSQL (``postgresql+asyncpg://``).
``BackboneDB`` owns the engine's lifecycle; query logic lives in the
repository modules, which receive connections from it.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

SQLITE_FILENAME = "backbone.db"
_POSTGRES_POOL_SIZE = 5
_POSTGRES_POOL_OVERFLOW = 10


def sqlite_url(data_dir: Path) -> str:
    """The default database URL: a SQLite file in the data directory."""
    return f"sqlite+aiosqlite:///{data_dir / SQLITE_FILENAME}"


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def is_memory(url: str) -> bool:
    return ":memory:" in url


def build_engine(url: str) -> AsyncEngine:
    """Create an async engine with dialect-appropriate pool settings."""
    if is_sqlite(url):
        if not is_memory(url):
            db_file = url.split("///", 1)[-1]
            Path(db_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        return create_async_engine(url)
    return create_async_engine(
        url,
        pool_size=_POSTGRES_POOL_SIZE,
        max_overflow=_POSTGRES_POOL_OVERFLOW,
        pool_pre_ping=True,
    )


def redact_url(url: str) -> str:
    """Hide credentials in a database URL for logging.

    Drops the query string too — connection parameters can carry passwords
    or tokens (``?password=…``) that must not reach logs or ``/health``.
    """
    base, _, query = url.partition("?")
    if "@" in base and "://" in base:
        scheme, rest = base.split("://", 1)
        _creds, host = rest.rsplit("@", 1)
        base = f"{scheme}://***@{host}"
    return f"{base}?***" if query else base
