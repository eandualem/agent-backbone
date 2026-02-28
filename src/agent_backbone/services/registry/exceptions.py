"""Registry service exceptions."""

from agent_backbone.base import BackboneError


class RegistryError(BackboneError):
    category = "registry"
    severity = "high"
    retry_allowed = False
