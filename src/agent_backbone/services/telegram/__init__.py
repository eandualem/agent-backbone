"""Telegram service — bot interface for agent management."""

from agent_backbone.services.telegram._routing import _delivery_reply
from agent_backbone.services.telegram.exceptions import TelegramServiceError
from agent_backbone.services.telegram.interface import (
    TelegramService,
    send_notification,
)

__all__ = [
    "TelegramService",
    "TelegramServiceError",
    "_delivery_reply",
    "send_notification",
]
