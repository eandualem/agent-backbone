"""Shared test fixtures for agent-backbone."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.config import BackboneConfig, GatewayConfig, GitHubConfig
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
        webhook_secret="test-secret",
        gateway=GatewayConfig(port=9877),
        github=GitHubConfig(owner="eandualem", repo="orchestration"),
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


@pytest.fixture
def api_app(config, tmp_path):
    """Create a FastAPI app with test config and in-memory DB."""
    from api.app import create_app

    app = create_app()
    # Override config with test config using in-memory DB
    from dataclasses import replace

    from src.config import DeliveryConfig

    test_db_path = str(tmp_path / "test.db")
    test_config = replace(config, delivery=DeliveryConfig(db_path=test_db_path))
    app.state.config = test_config
    return app


@pytest.fixture
async def api_client(api_app):
    """Async test client for the FastAPI app."""
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def api_key():
    """Set and return a test API key."""
    key = "test-api-key-123"
    os.environ["BACKBONE_API_KEY"] = key
    yield key
    os.environ.pop("BACKBONE_API_KEY", None)


@pytest.fixture
def auth_headers(api_key):
    """Authorization headers with test API key."""
    return {"Authorization": f"Bearer {api_key}"}
