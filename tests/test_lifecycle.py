"""Tests for flows/lifecycle.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from flows.lifecycle import on_issue_closed
from src.models import EventType, IssueData, IssueEvent, ParsedLabels


def make_close_event(targets: list[str]) -> IssueEvent:
    labels = ParsedLabels(sender="ike", targets=targets, issue_type="task")
    issue = IssueData(number=10, title="[task] Done", state="closed", labels=labels)
    return IssueEvent(event_type=EventType.ISSUE_CLOSED, issue=issue)


class TestOnIssueClosed:
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
            ) as mock_find,
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
