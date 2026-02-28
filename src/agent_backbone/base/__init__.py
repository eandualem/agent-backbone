"""Foundation layer — protocols, lifecycle management, and exception hierarchy."""

from agent_backbone.base.exceptions import (
    BackboneError,
    ConfigurationError,
    DeliveryError,
    ExternalServiceError,
    StateError,
)
from agent_backbone.base.lifecycle import LifecycleManager
from agent_backbone.base.protocols import LifecycleAware

__all__ = [
    "BackboneError",
    "ConfigurationError",
    "DeliveryError",
    "ExternalServiceError",
    "LifecycleAware",
    "LifecycleManager",
    "StateError",
]
