"""Tests for flows/issue_dispatcher.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from flows.issue_dispatcher import DispatchResult, issue_dispatcher, resolve_session
from src.models import (
    CommentData,
    EventType,
    IssueData,
    IssueEvent,
    ParsedLabels,
)


class TestResolveSession:
    async def test_named_entity(self):
        result = await resolve_session.fn("ike", "irrelevant title")
        assert result == "ike"

    async def test_unknown_entity(self):
        result = await resolve_session.fn("nobody", "irrelevant title")
        assert result is None

    async def test_coding_agent_with_repo_session(self):
        with patch(
            "flows.issue_dispatcher.session_exists",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await resolve_session.fn("coding-agent", "[task] platform-api: Fix bug")
            assert result == "platform-api"

    async def test_coding_agent_repo_offline_falls_back(self):
        with patch(
            "flows.issue_dispatcher.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await resolve_session.fn("coding-agent", "[task] platform-api: Fix bug")
            assert result == "ike"

    async def test_coding_agent_no_repo_in_title(self):
        result = await resolve_session.fn("coding-agent", "Some random title")
        assert result == "ike"


class TestIssueDispatcher:
    async def test_dispatch_to_named_entity(self):
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=1, title="[task] Do thing", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            patch(
                "flows.issue_dispatcher.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "flows.issue_dispatcher.send_message",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_send,
        ):
            result = await issue_dispatcher.fn(event)

        assert "ike" in result.delivered
        assert mock_send.called

    async def test_skip_elias(self):
        labels = ParsedLabels(sender="ike", targets=["elias"], issue_type="question")
        issue = IssueData(number=2, title="[question] Clarify", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        result = await issue_dispatcher.fn(event)
        assert "elias" in result.skipped
        assert result.delivered == []

    async def test_session_offline(self):
        labels = ParsedLabels(sender="leo", targets=["feynman"], issue_type="task")
        issue = IssueData(number=3, title="[task] Something", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            patch(
                "flows.issue_dispatcher.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "flows.issue_dispatcher.send_message",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await issue_dispatcher.fn(event)

        assert "feynman" in result.offline

    async def test_comment_event(self):
        labels = ParsedLabels(sender="leo", targets=["ike"], issue_type="task")
        issue = IssueData(number=4, title="[task] Something", labels=labels)
        comment = CommentData(body="Test comment", user_login="eandualem")
        event = IssueEvent(
            event_type=EventType.COMMENT_CREATED, issue=issue, comment=comment
        )

        with (
            patch(
                "flows.issue_dispatcher.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "flows.issue_dispatcher.send_message",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await issue_dispatcher.fn(event)

        assert "ike" in result.delivered

    async def test_multiple_targets(self):
        labels = ParsedLabels(sender="leo", targets=["ike", "feynman"], issue_type="task")
        issue = IssueData(number=5, title="[task] Both", labels=labels)
        event = IssueEvent(event_type=EventType.ISSUE_OPENED, issue=issue)

        with (
            patch(
                "flows.issue_dispatcher.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "flows.issue_dispatcher.send_message",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await issue_dispatcher.fn(event)

        assert len(result.delivered) == 2

    async def test_ignores_unknown_event(self):
        labels = ParsedLabels(sender="leo", targets=["ike"])
        issue = IssueData(number=6, title="Whatever", labels=labels)
        event = IssueEvent(event_type=EventType.UNKNOWN, issue=issue)

        result = await issue_dispatcher.fn(event)
        assert result.delivered == []
        assert result.offline == []
