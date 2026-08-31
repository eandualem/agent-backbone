"""Alembic environment configuration for agent-backbone.

Supports two modes:
1. CLI: ``alembic upgrade head`` — resolves the database URL from the data directory
   / ``BACKBONE_DATABASE_URL`` and creates an async engine.
2. Programmatic: ``BackboneDB._run_migrations()`` injects a connection via
   ``config.attributes["connection"]``.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from agent_backbone.services.database.base import Base
from agent_backbone.services.database.models import (  # noqa: F401
    AcknowledgmentORM,
    AgentORM,
    AgentStateORM,
    AgentWatchORM,
    DedupLogORM,
    DeliveryORM,
    EventORM,
    IssueDependencyORM,
    MessageQueueORM,
    SettingORM,
)
from alembic import context

metadata = Base.metadata
target_metadata = metadata

config = context.config

if config.attributes.get("connection") is None and not config.get_main_option("sqlalchemy.url"):
    from agent_backbone.config import bootstrap_config

    config.set_main_option("sqlalchemy.url", bootstrap_config().database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generate SQL script)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:  # noqa: ANN001
    """Configure context and run migrations within a connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """CLI mode — create async engine and run migrations."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connected to database)."""
    connectable = config.attributes.get("connection")

    if connectable is not None:
        _do_run_migrations(connectable)
    else:
        asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
