"""Routing service exceptions — dispatch, delivery, and notification errors."""

from agent_backbone.base import BackboneError


class RoutingError(BackboneError):
    """Base routing error."""

    category = "routing"
    severity = "high"
    retry_allowed = True


class DispatchError(RoutingError):
    """Dispatch coordination error."""

    category = "dispatch"


class DeliveryServiceError(RoutingError):
    """Delivery coordination error."""

    category = "delivery"


class NotificationError(RoutingError):
    """Notification formatting error."""

    category = "notification"
    severity = "low"
    retry_allowed = False
