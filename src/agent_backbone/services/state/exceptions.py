"""Agent state service exceptions."""

from agent_backbone.base import BackboneError


class AgentStateError(BackboneError):
    """Agent state tracking error."""

    category = "state"
    severity = "medium"
    retry_allowed = False
