"""Workflow service exceptions."""

from agent_backbone.base.exceptions import BackboneError


class WorkflowServiceError(BackboneError):
    """Raised when a workflow operation fails."""
