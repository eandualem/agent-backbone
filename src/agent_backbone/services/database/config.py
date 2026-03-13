"""Database configuration — Pydantic model with computed URLs."""

from __future__ import annotations

import os

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


_DATABASE_ENV_FIELDS = {
    "host": "BACKBONE_DATABASE_HOST",
    "port": "BACKBONE_DATABASE_PORT",
    "user": "BACKBONE_DATABASE_USER",
    "password": "BACKBONE_DATABASE_PASSWORD",
    "name": "BACKBONE_DATABASE_NAME",
    "pool_size": "BACKBONE_DATABASE_POOL_SIZE",
    "pool_overflow": "BACKBONE_DATABASE_POOL_OVERFLOW",
    "echo": "BACKBONE_DATABASE_ECHO",
}


def database_config_from_env(*, defaults: DatabaseConfig | None = None) -> DatabaseConfig:
    """Build DatabaseConfig with BACKBONE_DATABASE_* env overrides applied."""
    base = defaults or DatabaseConfig()
    updates: dict[str, object] = {}
    for field_name, env_name in _DATABASE_ENV_FIELDS.items():
        value = os.getenv(env_name)
        if value not in (None, ""):
            updates[field_name] = value
    if not updates:
        return base
    return DatabaseConfig.model_validate({**base.model_dump(), **updates})
