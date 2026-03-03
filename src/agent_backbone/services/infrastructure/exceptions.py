"""Infrastructure service exceptions."""

from agent_backbone.base import BackboneError


class InfrastructureError(BackboneError):
    """Infrastructure operation error."""

    category = "infrastructure"
    severity = "high"
    retry_allowed = False


class PortInUseError(InfrastructureError):
    """A required port is already occupied."""


class ServiceStartError(InfrastructureError):
    """Failed to start an infrastructure service."""


class ServiceStopError(InfrastructureError):
    """Failed to stop an infrastructure service."""
