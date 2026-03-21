"""GitHub service exceptions."""

from __future__ import annotations

from agent_backbone.base import ExternalServiceError


class GitHubServiceError(ExternalServiceError):
    """GitHub API error with upstream metadata."""

    category = "github"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        response_text: str | None = None,
        retry_allowed: bool | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            response_text=response_text,
        )
        if retry_allowed is not None:
            self.retry_allowed = retry_allowed
