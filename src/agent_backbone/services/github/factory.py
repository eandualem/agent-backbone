"""GitHub service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.github.interface import GitHubClient


async def register_github(
    lifecycle: LifecycleManager,
    config: BackboneConfig,
) -> GitHubClient:
    """Create and register the GitHub client service."""
    from agent_backbone.services.github.interface import GitHubClient

    service = GitHubClient(config)
    lifecycle.register("github", service)
    return service
