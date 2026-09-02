"""Priority scoring for issue queue ordering.

Pure scoring function — no I/O. Computes a float score based on issue type,
blocking status, downstream dependents, and age. Higher score = higher priority.
"""

from __future__ import annotations

from agent_backbone.config import PriorityConfig
from agent_backbone.models import IssueData


def compute_priority_score(
    issue: IssueData,
    config: PriorityConfig,
    dependents_count: int = 0,
) -> float:
    """Compute a priority score for an issue.

    Score = base_type_score + blocking_bonus + dependents_bonus + age_bonus
    """
    base = config.type_weights.get(issue.labels.issue_type, 0.0)

    blocking_bonus = config.blocking_weight if issue.labels.blocking else 0.0

    if dependents_count > 0:
        dependents_bonus = base * (config.dependents_multiplier**dependents_count - 1)
    else:
        dependents_bonus = 0.0

    age_bonus = max(0, 10000 - issue.number) * config.age_tiebreaker_weight

    return base + blocking_bonus + dependents_bonus + age_bonus
