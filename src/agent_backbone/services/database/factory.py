"""Database service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database.backbone_db import BackboneDB
    from agent_backbone.services.database.interface import DatabaseService


async def register_database(
    lifecycle: LifecycleManager,
    config: BackboneConfig,
) -> DatabaseService:
    """Create and register the database service."""
    from agent_backbone.services.database.interface import DatabaseService

    service = DatabaseService(config.database_url, config.database)
    lifecycle.register("database", service)
    return service


async def register_persistence(
    lifecycle: LifecycleManager,
    database_service: DatabaseService,
) -> BackboneDB:
    """Create and register the persistence service."""
    from agent_backbone.services.database.backbone_db import BackboneDB

    service = BackboneDB(database_service=database_service)
    lifecycle.register("persistence", service)
    return service
