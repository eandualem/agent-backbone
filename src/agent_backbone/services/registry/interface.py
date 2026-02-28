"""Entity registry service — LifecycleAware wrapper."""

from __future__ import annotations

import logging

from agent_backbone.services.registry.models import EntityRegistry

log = logging.getLogger(__name__)


class RegistryService:
    """Entity registry service implementing LifecycleAware."""

    def __init__(self, registry: EntityRegistry | None = None) -> None:
        self._registry = registry or EntityRegistry()

    @property
    def registry(self) -> EntityRegistry:
        return self._registry

    async def start(self) -> None:
        log.info(
            "Registry service started: %d entities, %d repos",
            len(self._registry.entities),
            len(self._registry.repos),
        )

    async def stop(self) -> None:
        pass

    async def health_check(self) -> dict:
        return {
            "healthy": True,
            "service": "registry",
            "entity_count": len(self._registry.entities),
            "repo_count": len(self._registry.repos),
        }
