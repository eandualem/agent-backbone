"""Exception hierarchy for the agent backbone.

Structured exceptions with metadata for observability and retry decisions.
"""

from __future__ import annotations


class BackboneError(Exception):
    """Base exception for all backbone errors.

    Carries metadata for observability: category, severity, and retry eligibility.
    """

    category: str = "general"
    severity: str = "medium"
    retry_allowed: bool = False

    def __init__(self, message: str, **kwargs):
        super().__init__(message)
        self.message = message
        for key, value in kwargs.items():
            setattr(self, key, value)


class ConfigurationError(BackboneError):
    """Invalid or missing configuration. Not retryable."""

    category = "configuration"
    severity = "critical"
    retry_allowed = False


class DeliveryError(BackboneError):
    """Failed to deliver a notification to an agent session. Retryable."""

    category = "delivery"
    severity = "high"
    retry_allowed = True


class StateError(BackboneError):
    """Invalid state transition or state file corruption. Not retryable."""

    category = "state"
    severity = "medium"
    retry_allowed = False


class ExternalServiceError(BackboneError):
    """External service (GitHub API, Telegram) unavailable. Retryable."""

    category = "external"
    severity = "high"
    retry_allowed = True
