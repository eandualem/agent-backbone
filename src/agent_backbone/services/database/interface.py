"""Database service — engine lifecycle and session management."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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


class DatabaseService:
    """Database connection management — LifecycleAware.

    Owns engine lifecycle and session factory. Query logic lives in
    repository modules which receive sessions from this service.
    """

    def __init__(self, config: DatabaseConfig) -> None:
        self._config = config
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine | None:
        """Expose engine for health checks and direct connection access."""
        return self._engine

    async def start(self) -> None:
        """Create engine, verify connectivity, build session factory."""
        url = self._config.async_url
        self._engine = create_async_engine(
            url,
            pool_size=self._config.pool_size,
            max_overflow=self._config.pool_overflow,
            echo=self._config.echo,
            pool_pre_ping=True,
        )
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        log.info(
            "Database connected: %s:%d/%s",
            self._config.host,
            self._config.port,
            self._config.name,
        )

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
        }

    async def get_session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session — for FastAPI Depends()."""
        if not self._session_factory:
            raise DatabaseError("Database not started")
        async with self._session_factory() as session:
            yield session

    @asynccontextmanager
    async def session_context(self) -> AsyncIterator[AsyncSession]:
        """Session context manager — for background tasks and flows."""
        if not self._session_factory:
            raise DatabaseError("Database not started")
        async with self._session_factory() as session:
            yield session
