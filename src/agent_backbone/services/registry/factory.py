"""Registry service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.registry.interface import RegistryService
    from agent_backbone.services.registry.models import EntityRegistry


async def register_registry(
    lifecycle: LifecycleManager,
    registry: EntityRegistry | None = None,
) -> RegistryService:
    from agent_backbone.services.registry.interface import RegistryService

    service = RegistryService(registry=registry)
    lifecycle.register("registry", service)
    return service
