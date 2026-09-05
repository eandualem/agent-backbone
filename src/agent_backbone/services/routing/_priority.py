"""Priority scoring for issue queue ordering.

Pure scoring function — no I/O. Computes a float score based on issue type,
blocking status, downstream dependents, and age. Higher score = higher priority.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent_backbone.config import PriorityConfig
from agent_backbone.models import IssueData


def compute_priority_score(
    issue: IssueData,
    config: PriorityConfig,
    dependents_count: int = 0,
    *,
    now: datetime | None = None,
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

    age_days = 0.0
    if issue.created_at:
        try:
            created = datetime.fromisoformat(issue.created_at.replace("Z", "+00:00"))
            if created.tzinfo is not None:
                age_days = max(0.0, ((now or datetime.now(UTC)) - created).total_seconds() / 86400)
        except (ValueError, OverflowError):
            pass  # Unknown creation time gets no age bonus, never a number proxy.
    age_bonus = age_days * config.age_tiebreaker_weight

    return base + blocking_bonus + dependents_bonus + age_bonus
