"""Database service — engine lifecycle and session management."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent_backbone.services.database.config import DatabaseConfig
from agent_backbone.services.database.exceptions import DatabaseError

log = logging.getLogger(__name__)


def build_engine(url: str, config: DatabaseConfig | None = None) -> AsyncEngine:
    """Create an async engine with dialect-appropriate pool settings."""
    config = config or DatabaseConfig()
    if DatabaseConfig.is_sqlite(url):
        if not DatabaseConfig.is_memory(url):
            db_file = url.split("///", 1)[-1]
            Path(db_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        return create_async_engine(url, echo=config.echo)
    return create_async_engine(
        url,
        pool_size=config.pool_size,
        max_overflow=config.pool_overflow,
        echo=config.echo,
        pool_pre_ping=True,
    )


def redact_url(url: str) -> str:
    """Hide credentials in a database URL for logging."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    _creds, host = rest.rsplit("@", 1)
    return f"{scheme}://***@{host}"


class DatabaseService:
    """Database connection management — LifecycleAware.

    Owns engine lifecycle and session factory. Query logic lives in
    repository modules which receive connections from this engine.
    """

    def __init__(self, url: str, config: DatabaseConfig | None = None) -> None:
        self._url = url
        self._config = config or DatabaseConfig()
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def url(self) -> str:
        return self._url

    @property
    def engine(self) -> AsyncEngine | None:
        """Expose engine for health checks and direct connection access."""
        return self._engine

    async def start(self) -> None:
        """Create engine, verify connectivity, build session factory."""
        self._engine = build_engine(self._url, self._config)
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        log.info("Database connected: %s", redact_url(self._url))

    async def stop(self) -> None:
        """Dispose of the engine and session factory."""
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    async def health_check(self) -> dict:
        """Check database connectivity."""
        healthy = False
        if self._engine:
            try:
                async with self._engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                healthy = True
            except Exception:
                pass
        return {
            "healthy": healthy,
            "service": "database",
            "connected": self._engine is not None,
            "url": redact_url(self._url),
        }

    async def get_session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session — for FastAPI Depends()."""
        if not self._session_factory:
            raise DatabaseError("Database not started")
        async with self._session_factory() as session:
            yield session

    @asynccontextmanager
    async def session_context(self) -> AsyncIterator[AsyncSession]:
        """Session context manager — for background tasks."""
        if not self._session_factory:
            raise DatabaseError("Database not started")
        async with self._session_factory() as session:
            yield session
