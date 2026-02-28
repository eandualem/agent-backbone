"""State service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.state.interface import StateService


async def register_state(
    lifecycle: LifecycleManager,
    config: BackboneConfig,
) -> StateService:
    """Create and register the state service."""
    from agent_backbone.services.state.interface import StateService

    service = StateService(
        state_dir=config.agent_state.state_dir,
        stale_threshold=config.agent_state.stale_threshold_seconds,
    )
    lifecycle.register("state", service)
    return service
