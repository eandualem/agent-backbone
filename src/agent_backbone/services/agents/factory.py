"""Agents service factory — state service registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.agents.interface import StateService
    from agent_backbone.services.database import BackboneDB


async def register_state(
    lifecycle: LifecycleManager,
    config: BackboneConfig,
    db: BackboneDB | None = None,
) -> StateService:
    """Create and register the state service."""
    from agent_backbone.services.agents.interface import StateService

    service = StateService(
        state_dir=config.state_dir,
        stale_threshold=config.agent_state.stale_threshold_seconds,
        db=db,
    )
    lifecycle.register("state", service)
    return service
