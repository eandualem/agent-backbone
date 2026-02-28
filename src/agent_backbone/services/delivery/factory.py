"""Delivery service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.delivery.interface import DeliveryService


async def register_delivery(lifecycle: LifecycleManager) -> DeliveryService:
    """Create and register the delivery service."""
    from agent_backbone.services.delivery.interface import DeliveryService

    service = DeliveryService()
    lifecycle.register("delivery", service)
    return service
