"""Automation service exceptions."""

from agent_backbone.base.exceptions import BackboneError


class OnboardingError(BackboneError):
    """Raised when an onboarding operation fails."""


class WorkflowServiceError(BackboneError):
    """Raised when a workflow operation fails."""
