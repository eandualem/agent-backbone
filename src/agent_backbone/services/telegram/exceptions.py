"""Telegram service exceptions."""

from agent_backbone.base import ExternalServiceError


class TelegramServiceError(ExternalServiceError):
    """Telegram bot/API error."""

    category = "telegram"
