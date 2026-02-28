"""GitHub service — async REST API client for issue operations."""

from agent_backbone.services.github.exceptions import GitHubServiceError
from agent_backbone.services.github.interface import API_BASE, GitHubClient

__all__ = [
    "API_BASE",
    "GitHubClient",
    "GitHubServiceError",
]
