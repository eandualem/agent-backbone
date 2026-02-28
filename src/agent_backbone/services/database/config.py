"""Database configuration — Pydantic model with computed URLs."""

from __future__ import annotations

from pydantic import BaseModel, computed_field


class DatabaseConfig(BaseModel):
    """PostgreSQL connection settings."""

    host: str = "localhost"
    port: int = 5435
    user: str = "backbone"
    password: str = "backbone"
    name: str = "backbone"
    pool_size: int = 5
    pool_overflow: int = 10
    echo: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def async_url(self) -> str:
        """SQLAlchemy async URL for asyncpg."""
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_url(self) -> str:
        """SQLAlchemy sync URL for Alembic CLI."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"
