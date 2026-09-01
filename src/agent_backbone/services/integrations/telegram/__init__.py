"""Telegram service — bot commands, topic routing and notifications."""

from agent_backbone.services.integrations.telegram.interface import (
    TelegramService,
    send_notification,
)

__all__ = ["TelegramService", "send_notification"]
