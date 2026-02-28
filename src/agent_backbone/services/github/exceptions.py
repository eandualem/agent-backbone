"""GitHub service exceptions."""

from agent_backbone.base import ExternalServiceError


class GitHubServiceError(ExternalServiceError):
    """GitHub API error."""

    category = "github"
