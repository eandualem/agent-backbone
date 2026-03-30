"""Pure priority scoring helpers for canonical issue projections."""

from __future__ import annotations

from agent_backbone.config import PriorityScoringConfig
from agent_backbone.models import IssueData


def compute_priority_score(
    issue: IssueData,
    config: PriorityScoringConfig,
    dependents_count: int = 0,
) -> float:
    """Compute the configured weighted score for one issue."""
    base_type_score = float(config.type_weights.get(issue.labels.issue_type, 0.0))
    blocking_bonus = float(config.blocking_weight) if issue.labels.priority == "blocking" else 0.0
    dependents_bonus = 0.0
    if dependents_count > 0:
        dependents_bonus = base_type_score * (
            float(config.dependents_multiplier) ** dependents_count - 1.0
        )
    age_bonus = (10000 - int(issue.number)) * float(config.age_tiebreaker_weight)
    return base_type_score + blocking_bonus + dependents_bonus + age_bonus
