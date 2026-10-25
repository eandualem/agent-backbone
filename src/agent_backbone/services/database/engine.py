"""Engine construction.

SQLite (a file in the data directory) is the default and needs no setup.
``BACKBONE_DATABASE_URL`` can point at PostgreSQL (``postgresql+asyncpg://``).
``BackboneDB`` owns the engine's lifecycle; query logic lives in the
repository modules, which receive connections from it.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_POSTGRES_POOL_SIZE = 5
_POSTGRES_POOL_OVERFLOW = 10


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def is_memory(url: str) -> bool:
    return ":memory:" in url


def build_engine(url: str) -> AsyncEngine:
    """Create an async engine with dialect-appropriate pool settings."""
    if is_sqlite(url):
        if not is_memory(url):
            db_file = make_url(url).database
            if db_file:
                path = Path(db_file).expanduser()
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
                for suffix in ("-wal", "-shm", "-journal"):
                    companion = Path(str(path) + suffix)
                    if companion.exists() and not companion.is_symlink():
                        companion.chmod(0o600)
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
