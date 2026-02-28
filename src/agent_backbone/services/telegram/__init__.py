"""Telegram service — bot interface for agent management."""

from agent_backbone.services.telegram._flows import get_delivery_queue, get_system_status
from agent_backbone.services.telegram._routing import _delivery_reply
from agent_backbone.services.telegram.exceptions import TelegramServiceError
from agent_backbone.services.telegram.interface import (
    TelegramService,
    send_notification,
)

# Backward compatibility alias
BackboneBot = TelegramService

__all__ = [
    "BackboneBot",
    "TelegramService",
    "TelegramServiceError",
    "_delivery_reply",
    "get_delivery_queue",
    "get_system_status",
    "send_notification",
]
