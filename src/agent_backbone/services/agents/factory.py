"""Agents service factory — state + monitoring registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.agents.interface import MonitoringService, StateService
    from agent_backbone.services.database import BackboneDB


async def register_state(
    lifecycle: LifecycleManager,
    config: BackboneConfig,
    db: BackboneDB | None = None,
) -> StateService:
    """Create and register the state service."""
    from agent_backbone.services.agents.interface import StateService

    service = StateService(
        state_dir=config.agent_state.state_dir,
        stale_threshold=config.agent_state.stale_threshold_seconds,
        db=db,
    )
    lifecycle.register("state", service)
    return service


async def register_monitoring(
    lifecycle: LifecycleManager, config: BackboneConfig
) -> MonitoringService:
    """Create and register the monitoring service."""
    from agent_backbone.services.agents.interface import MonitoringService

    service = MonitoringService(schedule_path=config.heartbeat.schedule_path)
    lifecycle.register("monitoring", service)
    return service
