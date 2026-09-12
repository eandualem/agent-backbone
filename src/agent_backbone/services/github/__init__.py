"""GitHub service — async REST client for issues, comments and polling."""

from agent_backbone.services.github.interface import API_BASE, GitHubClient
from agent_backbone.services.github.preflight import actions_checker
from agent_backbone.services.github.reviews import review_started_event, review_status_event
from agent_backbone.services.github.snapshot import QueueSnapshot

__all__ = [
    "API_BASE",
    "GitHubClient",
    "QueueSnapshot",
    "actions_checker",
    "review_started_event",
    "review_status_event",
]
