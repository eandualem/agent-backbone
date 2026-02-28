"""Delivery service exceptions."""

from agent_backbone.base import BackboneError


class DeliveryServiceError(BackboneError):
    """Delivery coordination error."""

    category = "delivery"
    severity = "high"
    retry_allowed = True
