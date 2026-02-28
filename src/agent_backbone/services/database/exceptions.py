"""Database service exceptions."""

from agent_backbone.base import BackboneError


class DatabaseError(BackboneError):
    """Database connection or query error."""

    category = "database"
    severity = "high"
    retry_allowed = True
