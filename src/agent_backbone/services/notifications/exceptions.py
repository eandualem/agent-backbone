"""Notification service exceptions."""

from agent_backbone.base import BackboneError


class NotificationError(BackboneError):
    category = "notification"
    severity = "low"
    retry_allowed = False
