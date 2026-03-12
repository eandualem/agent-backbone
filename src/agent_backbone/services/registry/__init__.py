"""Registry service — entity and repo discovery."""

from agent_backbone.services.registry._loader import (
    build_registry,
    discover_coding_repos,
    load_entity_registry,
)
from agent_backbone.services.registry.exceptions import RegistryError
from agent_backbone.services.registry.interface import RegistryService
from agent_backbone.services.registry.models import (
    EntityEntry,
    EntityInstance,
    EntityRegistry,
    RepoInfo,
)

__all__ = [
    "EntityEntry",
    "EntityInstance",
    "EntityRegistry",
    "RegistryError",
    "RegistryService",
    "RepoInfo",
    "build_registry",
    "discover_coding_repos",
    "load_entity_registry",
]
