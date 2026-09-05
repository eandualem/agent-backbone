"""GitHub service — async REST client for issues, comments and polling."""

from agent_backbone.services.github.interface import API_BASE, GitHubClient
from agent_backbone.services.github.reviews import review_started_event
from agent_backbone.services.github.snapshot import QueueSnapshot

__all__ = ["API_BASE", "GitHubClient", "QueueSnapshot", "review_started_event"]
