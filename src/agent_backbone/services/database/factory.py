"""Database service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database.interface import DatabaseService


async def register_database(
    lifecycle: LifecycleManager,
    config: BackboneConfig,
) -> DatabaseService:
    """Create and register the database service."""
    from agent_backbone.services.database.interface import DatabaseService

    service = DatabaseService(config.database)
    lifecycle.register("database", service)
    return service
