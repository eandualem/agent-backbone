"""Agent service exceptions — combined state + monitoring."""

from agent_backbone.base import BackboneError


class AgentStateError(BackboneError):
    """Agent state tracking error."""

    category = "state"
    severity = "medium"
    retry_allowed = False


class MonitoringError(BackboneError):
    """Monitoring coordination error."""

    category = "monitoring"
    severity = "high"
    retry_allowed = True
