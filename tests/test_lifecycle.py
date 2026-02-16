"""Tests for flows/lifecycle.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from flows.lifecycle import find_next_issue, on_issue_closed
from src.dedup import clear as clear_dedup
from src.models import EventType, IssueData, IssueEvent, ParsedLabels


def make_close_event(targets: list[str]) -> IssueEvent:
    labels = ParsedLabels(sender="ike", targets=targets, issue_type="task")
    issue = IssueData(number=10, title="[task] Done", state="closed", labels=labels)
    return IssueEvent(event_type=EventType.ISSUE_CLOSED, issue=issue)


class TestOnIssueClosed:
    def setup_method(self):
        clear_dedup()

    async def test_delivers_next_issue(self):
        event = make_close_event(["feynman"])
        next_issue = IssueData(
            number=11,
            title="[task] Next thing",
            labels=ParsedLabels(sender="leo", targets=["feynman"], issue_type="task"),
        )

        with (
            patch(
                "flows.lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "flows.lifecycle.find_next_issue",
                new_callable=AsyncMock,
                return_value=next_issue,
            ),
            patch(
                "flows.lifecycle.send_message",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_send,
        ):
            # Call the flow function directly
            result = await on_issue_closed.fn(event)

        assert result["feynman"] == "delivered_#11"
        mock_send.assert_called_once()

    async def test_queue_empty(self):
        event = make_close_event(["feynman"])

        with (
            patch(
                "flows.lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "flows.lifecycle.find_next_issue",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await on_issue_closed.fn(event)

        assert result["feynman"] == "queue_empty"

    async def test_session_offline(self):
        event = make_close_event(["feynman"])

        with patch(
            "flows.lifecycle.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await on_issue_closed.fn(event)

        assert result["feynman"] == "offline"

    async def test_skips_elias(self):
        event = make_close_event(["elias"])
        result = await on_issue_closed.fn(event)
        assert result["elias"] == "skipped"

    async def test_blocking_issues_first(self):
        """Verify that find_next_issue returns blocking issues first.

        This tests the GitHub client's sorting — verified via integration
        in test_github.py. Here we verify the flow passes through correctly.
        """
        event = make_close_event(["ike"])
        blocking_issue = IssueData(
            number=20,
            title="[bug] Blocking",
            labels=ParsedLabels(
                sender="ada", targets=["ike"], issue_type="bug", priority="blocking"
            ),
        )

        with (
            patch(
                "flows.lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "flows.lifecycle.find_next_issue",
                new_callable=AsyncMock,
                return_value=blocking_issue,
            ),
            patch(
                "flows.lifecycle.send_message",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await on_issue_closed.fn(event)

        assert result["ike"] == "delivered_#20"

    async def test_dedup_prevents_redelivery(self):
        """Closing two issues in a row shouldn't re-deliver the same next issue."""
        next_issue = IssueData(
            number=6,
            title="[task] Tmux theming",
            labels=ParsedLabels(sender="leo", targets=["feynman"], issue_type="task"),
        )

        with (
            patch(
                "flows.lifecycle.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "flows.lifecycle.find_next_issue",
                new_callable=AsyncMock,
                return_value=next_issue,
            ),
            patch(
                "flows.lifecycle.send_message",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_send,
        ):
            # First close: delivers #6
            event1 = make_close_event(["feynman"])
            result1 = await on_issue_closed.fn(event1)
            assert result1["feynman"] == "delivered_#6"

            # Second close: #6 is still next, but should be deduped
            event2 = make_close_event(["feynman"])
            result2 = await on_issue_closed.fn(event2)
            assert result2["feynman"] == "deduped_#6"

        # send_message should only be called once (first delivery)
        assert mock_send.call_count == 1

    async def test_find_next_issue_excludes_closed_number(self):
        """find_next_issue should filter out the just-closed issue number
        to avoid re-delivering it due to GitHub eventual consistency."""
        from src.config import BackboneConfig

        closed_issue = IssueData(
            number=10,
            title="[task] Just closed",
            labels=ParsedLabels(sender="ike", targets=["feynman"], issue_type="task"),
        )
        real_next = IssueData(
            number=11,
            title="[task] Actually next",
            labels=ParsedLabels(sender="leo", targets=["feynman"], issue_type="task"),
        )

        mock_gh = AsyncMock()
        # GitHub API returns both (closed one still appears as open)
        mock_gh.list_open_issues.return_value = [closed_issue, real_next]
        mock_gh.__aenter__ = AsyncMock(return_value=mock_gh)
        mock_gh.__aexit__ = AsyncMock(return_value=False)

        with patch("flows.lifecycle.GitHubClient", return_value=mock_gh):
            config = BackboneConfig(github_token="t", webhook_secret="s")
            result = await find_next_issue.fn(config, "feynman", exclude_number=10)

        assert result is not None
        assert result.number == 11
