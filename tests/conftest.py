"""Shared test fixtures for agent-backbone."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.config import BackboneConfig
from src.models import (
    CommentData,
    EventType,
    IssueData,
    IssueEvent,
    ParsedLabels,
)


@pytest.fixture
def config():
    """Test BackboneConfig with dummy values."""
    return BackboneConfig(
        github_token="test-token-123",
        github_owner="eandualem",
        github_repo="orchestration",
        gateway_port=9877,
        webhook_secret="test-secret",
    )


@pytest.fixture
def sample_labels():
    """Sample parsed labels."""
    return ParsedLabels(
        sender="leo",
        targets=["ike"],
        issue_type="task",
        priority="",
    )


@pytest.fixture
def sample_issue(sample_labels):
    """Sample issue data."""
    return IssueData(
        number=42,
        title="[task] Update config",
        state="open",
        labels=sample_labels,
        html_url="https://github.com/eandualem/orchestration/issues/42",
    )


@pytest.fixture
def sample_comment():
    """Sample comment data."""
    return CommentData(body="This is a test comment", user_login="eandualem")


@pytest.fixture
def sample_issue_event(sample_issue):
    """Sample issue opened event."""
    return IssueEvent(
        event_type=EventType.ISSUE_OPENED,
        issue=sample_issue,
        delivery_id="test-delivery-1",
    )


@pytest.fixture
def sample_comment_event(sample_issue, sample_comment):
    """Sample comment created event."""
    return IssueEvent(
        event_type=EventType.COMMENT_CREATED,
        issue=sample_issue,
        comment=sample_comment,
        delivery_id="test-delivery-2",
    )


@pytest.fixture
def sample_close_event():
    """Sample issue closed event."""
    labels = ParsedLabels(sender="ike", targets=["feynman"], issue_type="task")
    issue = IssueData(number=10, title="[task] Fix something", state="closed", labels=labels)
    return IssueEvent(event_type=EventType.ISSUE_CLOSED, issue=issue)


@pytest.fixture
def mock_tmux():
    """Mock tmux operations."""
    with (
        patch("src.tmux.session_exists", new_callable=AsyncMock) as mock_exists,
        patch("src.tmux.send_message", new_callable=AsyncMock) as mock_send,
        patch("src.tmux.list_sessions", new_callable=AsyncMock) as mock_list,
    ):
        mock_exists.return_value = True
        mock_send.return_value = True
        mock_list.return_value = ["feynman", "ike", "leo", "ada"]
        yield {
            "session_exists": mock_exists,
            "send_message": mock_send,
            "list_sessions": mock_list,
        }


@pytest.fixture
def github_issue_json():
    """Raw GitHub issue JSON as returned by the API."""
    return {
        "number": 42,
        "title": "[task] Update config",
        "state": "open",
        "html_url": "https://github.com/eandualem/orchestration/issues/42",
        "labels": [
            {"name": "from:leo"},
            {"name": "for:ike"},
            {"name": "task"},
        ],
    }


@pytest.fixture
def webhook_payload(github_issue_json):
    """Raw webhook payload for an issue opened event."""
    return {
        "action": "opened",
        "issue": github_issue_json,
    }
