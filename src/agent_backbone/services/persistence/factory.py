"""Persistence service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.database.interface import DatabaseService
    from agent_backbone.services.persistence.interface import BackboneDB


async def register_persistence(
    lifecycle: LifecycleManager,
    database_service: DatabaseService,
) -> BackboneDB:
    """Create and register the persistence service.

    Receives the engine from DatabaseService — single connection pool.
    """
    from agent_backbone.services.persistence.interface import BackboneDB

    service = BackboneDB(database_service=database_service)
    lifecycle.register("persistence", service)
    return service
