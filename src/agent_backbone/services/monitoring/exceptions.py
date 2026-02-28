"""Monitoring service exceptions."""

from agent_backbone.base import BackboneError


class MonitoringError(BackboneError):
    """Monitoring coordination error."""

    category = "monitoring"
    severity = "high"
    retry_allowed = True
