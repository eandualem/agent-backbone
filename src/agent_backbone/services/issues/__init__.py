"""Canonical issue-domain service exports."""

from agent_backbone.services.issues._priority import compute_priority_score
from agent_backbone.services.issues.interface import IssueService

__all__ = ["IssueService", "compute_priority_score"]
