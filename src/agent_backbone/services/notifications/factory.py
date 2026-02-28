"""Notification service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.notifications.interface import NotificationService


async def register_notifications(lifecycle: LifecycleManager) -> NotificationService:
    from agent_backbone.services.notifications.interface import NotificationService

    service = NotificationService()
    lifecycle.register("notifications", service)
    return service
