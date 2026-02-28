"""Monitoring service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.monitoring.interface import MonitoringService


async def register_monitoring(lifecycle: LifecycleManager) -> MonitoringService:
    """Create and register the monitoring service."""
    from agent_backbone.services.monitoring.interface import MonitoringService

    service = MonitoringService()
    lifecycle.register("monitoring", service)
    return service
