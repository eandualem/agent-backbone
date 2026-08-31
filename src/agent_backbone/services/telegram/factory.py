"""Telegram service factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_backbone.base import LifecycleManager
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.database import BackboneDB
    from agent_backbone.services.telegram.interface import TelegramService


async def register_telegram(
    lifecycle: LifecycleManager,
    config: BackboneConfig,
    db: BackboneDB | None = None,
) -> TelegramService:
    """Create and register the Telegram bot service."""
    from agent_backbone.services.telegram.interface import TelegramService

    service = TelegramService(config, db=db)
    lifecycle.register("telegram", service)
    return service
