"""Tests for routing/_priority.py and the queue order it produces."""

from __future__ import annotations

from agent_backbone.config import PriorityScoringConfig
from agent_backbone.models import IssueData, ParsedLabels
from agent_backbone.services.routing import compute_priority_score, list_open_queue_for_target


def _make_issue(
    number: int = 42,
    issue_type: str = "task",
    priority: str = "",
) -> IssueData:
    labels = ParsedLabels(sender="leo", targets=["ike"], issue_type=issue_type, priority=priority)
    return IssueData(number=number, title=f"[{issue_type}] Test", labels=labels)


class TestComputePriorityScore:
    def test_blocking_always_highest(self):
        config = PriorityScoringConfig()
        blocking = _make_issue(issue_type="spec-gap", priority="blocking")
        non_blocking = _make_issue(issue_type="spec-gap", priority="")

        score_blocking = compute_priority_score(blocking, config)
        score_non_blocking = compute_priority_score(non_blocking, config)

        assert score_blocking > score_non_blocking
        # Blocking bonus should dominate
        assert score_blocking - score_non_blocking >= 1000.0

    def test_type_weight_ordering(self):
        config = PriorityScoringConfig()
        types = ["spec-gap", "bug", "task", "question", "optimization"]
        scores = [compute_priority_score(_make_issue(issue_type=t), config) for t in types]

        # Each type should score higher than the next
        for i in range(len(scores) - 1):
            assert scores[i] > scores[i + 1], f"{types[i]} should score > {types[i + 1]}"

    def test_dependents_increase_score(self):
        config = PriorityScoringConfig()
        issue = _make_issue(issue_type="task")

        score_0 = compute_priority_score(issue, config, dependents_count=0)
        score_1 = compute_priority_score(issue, config, dependents_count=1)
        score_2 = compute_priority_score(issue, config, dependents_count=2)

        assert score_2 > score_1 > score_0

    def test_age_tiebreaker(self):
        config = PriorityScoringConfig()
        older = _make_issue(number=10, issue_type="task")
        newer = _make_issue(number=20, issue_type="task")

        score_older = compute_priority_score(older, config)
        score_newer = compute_priority_score(newer, config)

        assert score_older > score_newer

    def test_unknown_type_zero(self):
        config = PriorityScoringConfig()
        issue = _make_issue(issue_type="nonexistent")

        score = compute_priority_score(issue, config)

        # Only age tiebreaker contributes (base is 0)
        expected_age = (10000 - 42) * 0.01
        assert abs(score - expected_age) < 0.001

    def test_custom_config(self):
        config = PriorityScoringConfig(
            blocking_weight=500.0,
            type_weights={"task": 200.0},
            dependents_multiplier=2.0,
            age_tiebreaker_weight=0.0,
        )
        issue = _make_issue(number=100, issue_type="task", priority="blocking")

        score = compute_priority_score(issue, config, dependents_count=1)

        # base=200 + blocking=500 + dependents=200*(2^1 - 1)=200 + age=0
        assert abs(score - 900.0) < 0.001

    def test_high_issue_number_no_negative_age(self):
        """Issue numbers > 10000 should not produce negative age bonus."""
        config = PriorityScoringConfig(age_tiebreaker_weight=0.01)
        high = _make_issue(number=15000, issue_type="task")

        score = compute_priority_score(high, config)

        # base=50 + age=0 (clamped, not negative)
        assert score >= 50.0

    def test_zero_dependents_no_bonus(self):
        config = PriorityScoringConfig(age_tiebreaker_weight=0.0)
        issue = _make_issue(issue_type="task")

        score = compute_priority_score(issue, config, dependents_count=0)

        assert abs(score - 50.0) < 0.001


class TestQueueOrder:
    async def test_queue_is_sorted_by_priority_then_number(self, config):
        from unittest.mock import AsyncMock

        older = _make_issue(number=5, issue_type="task")
        blocking = _make_issue(number=8, issue_type="bug", priority="blocking")
        for issue in (older, blocking):
            issue.repo_full_name = "example/ike"
        gh = AsyncMock()
        gh.list_issues = AsyncMock(return_value=[older, blocking])  # GitHub's order

        queue = await list_open_queue_for_target(config, "ike", gh)

        assert [i.number for i in queue] == [8, 5]
