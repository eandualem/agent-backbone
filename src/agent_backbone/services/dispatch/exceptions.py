"""Dispatch service exceptions."""

from agent_backbone.base import BackboneError


class DispatchError(BackboneError):
    """Dispatch coordination error."""

    category = "dispatch"
    severity = "high"
    retry_allowed = True
