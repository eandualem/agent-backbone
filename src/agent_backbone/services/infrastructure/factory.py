"""Infrastructure service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.infrastructure.interface import InfrastructureService


async def register_infrastructure(
    lifecycle: LifecycleManager, config: BackboneConfig
) -> InfrastructureService:
    """Create and register the infrastructure service."""
    from agent_backbone.services.infrastructure.interface import InfrastructureService

    service = InfrastructureService(config)
    lifecycle.register("infrastructure", service)
    return service
