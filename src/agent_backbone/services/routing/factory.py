"""Routing service factories — dispatch, delivery, and notification registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.routing._format import NotificationService
    from agent_backbone.services.routing.interface import DeliveryService, DispatchService


async def register_dispatch(lifecycle: LifecycleManager) -> DispatchService:
    """Create and register the dispatch service."""
    from agent_backbone.services.routing.interface import DispatchService

    service = DispatchService()
    lifecycle.register("dispatch", service)
    return service


async def register_delivery(lifecycle: LifecycleManager) -> DeliveryService:
    """Create and register the delivery service."""
    from agent_backbone.services.routing.interface import DeliveryService

    service = DeliveryService()
    lifecycle.register("delivery", service)
    return service


async def register_notifications(lifecycle: LifecycleManager) -> NotificationService:
    """Create and register the notification service."""
    from agent_backbone.services.routing._format import NotificationService

    service = NotificationService()
    lifecycle.register("notifications", service)
    return service
