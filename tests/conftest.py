"""Shared test fixtures for agent-backbone."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agent_backbone.config import BackboneConfig, GatewayConfig, GitHubConfig
from agent_backbone.models import (
    CommentData,
    EventType,
    IssueData,
    IssueEvent,
    ParsedLabels,
)
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.registry import EntityEntry, EntityRegistry


@pytest.fixture(autouse=True)
def reset_flow_services():
    """Reset the flow service locator between tests."""
    from agent_backbone.services._locator import reset

    yield
    reset()


@pytest.fixture
def config():
    """Test BackboneConfig with dummy values."""
    test_registry = EntityRegistry(
        entities={
            "feynman": EntityEntry(
                session="feynman",
                home="~/orchestration",
                groups=["orchestrators"],
                figure="Richard Feynman",
                role="Orchestration Optimizer",
            ),
            "ike": EntityEntry(
                session="ike",
                home="~/ws/core/ike",
                groups=["orchestrators"],
                figure="Dwight Eisenhower",
                role="Core Orchestrator",
            ),
            "leo": EntityEntry(
                session="leo",
                home="~/ws/leo",
                groups=["orchestrators"],
                figure="Leonardo da Vinci",
                role="Strategy Co-Architect",
            ),
            "ada": EntityEntry(
                session="ada",
                home="~/ws/core/spec",
                groups=["standalone"],
                figure="Ada Lovelace",
                role="Spec Agent",
            ),
            "brunel": EntityEntry(
                session="brunel",
                home="~/infra",
                groups=["orchestrators"],
                figure="Isambard Kingdom Brunel",
                role="Infrastructure Agent",
            ),
            "hamilton": EntityEntry(
                session="hamilton",
                home="~/ws/core/hamilton",
                groups=["orchestrators"],
                figure="Alexander Hamilton",
                role="Arclio Orchestrator",
            ),
            "curie": EntityEntry(
                session="curie",
                home="~/ws/core/curie",
                groups=["orchestrators"],
                figure="Marie Curie",
                role="Loveble Orchestrator",
            ),
            "bell": EntityEntry(
                session="bell",
                home="~/ws/core/bell",
                groups=["orchestrators"],
                figure="Alexander Graham Bell",
                role="WF Orchestrator",
            ),
            "gallup": EntityEntry(
                session="gallup",
                home="~/ws/core/gallup",
                groups=["standalone"],
                figure="George Gallup",
                role="Market Research",
            ),
        },
        repos=[],
    )
    return BackboneConfig(
        github_token="test-token-123",
        webhook_secret="test-secret",
        gateway=GatewayConfig(port=7120),
        github=GitHubConfig(owner="eandualem", repo="orchestration"),
        registry=test_registry,
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
        repo_full_name="eandualem/orchestration",
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
        patch(
            "agent_backbone.services.terminal.interface.session_exists", new_callable=AsyncMock
        ) as mock_exists,
        patch(
            "agent_backbone.services.terminal.interface.send_message", new_callable=AsyncMock
        ) as mock_send,
        patch(
            "agent_backbone.services.terminal.interface.list_sessions", new_callable=AsyncMock
        ) as mock_list,
    ):
        mock_exists.return_value = True
        mock_send.return_value = True
        mock_list.return_value = [
            "feynman",
            "ike",
            "leo",
            "ada",
            "brunel",
            "hamilton",
            "curie",
            "bell",
            "gallup",
        ]
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
        "repository": {"full_name": "eandualem/orchestration"},
    }


@pytest.fixture
async def api_app(config, tmp_path):
    """Create a FastAPI app with test config and in-memory DB.

    create_app() returns a socketio.ASGIApp wrapping FastAPI.
    Tests need the inner FastAPI app for dependency_overrides and state.
    """
    from agent_backbone.api.app import create_app

    asgi_app = create_app()
    # Extract the inner FastAPI app from the socketio.ASGIApp wrapper
    app = asgi_app.other_asgi_app

    # Override config with test config using in-memory DB
    from dataclasses import replace

    from agent_backbone.base import LifecycleManager
    from agent_backbone.config import DeliveryConfig

    test_config = replace(
        config,
        delivery=DeliveryConfig(),
    )
    app.state.config = test_config

    # Wire lifecycle and services for tests
    from sqlalchemy.ext.asyncio import create_async_engine

    app.state.lifecycle = LifecycleManager()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    db = BackboneDB(engine)
    await db.start()
    app.state.db = db
    app.state.github = None  # Tests override via dependency_overrides when needed

    # Non-lifecycle services for DI
    from agent_backbone.services.automation import OnboardingService, WorkflowsService

    app.state.onboarding_service = OnboardingService()
    app.state.workflows_service = WorkflowsService()

    yield app

    db._engine = None
    await engine.dispose()


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
