"""Persistence service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.persistence.interface import BackboneDB


async def register_persistence(
    lifecycle: LifecycleManager,
    config: BackboneConfig,
) -> BackboneDB:
    """Create and register the persistence service."""
    from agent_backbone.services.persistence.interface import BackboneDB

    service = BackboneDB(
        config.database.async_url,
        pool_size=config.database.pool_size,
        max_overflow=config.database.pool_overflow,
    )
    lifecycle.register("persistence", service)
    return service
