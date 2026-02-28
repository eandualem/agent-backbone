"""Persistence service exceptions."""

from agent_backbone.base import BackboneError


class PersistenceError(BackboneError):
    """Database persistence error."""

    category = "persistence"
    severity = "high"
    retry_allowed = True
