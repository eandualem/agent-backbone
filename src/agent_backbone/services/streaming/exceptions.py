"""Streaming service exceptions."""

from agent_backbone.base import BackboneError


class StreamingServiceError(BackboneError):
    """Streaming service error."""

    category = "streaming"
    severity = "medium"
    retry_allowed = True
