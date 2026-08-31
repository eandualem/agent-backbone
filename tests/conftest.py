"""Shared test fixtures for agent-backbone."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agent_backbone.config import (
    AgentsConfig,
    AgentSpec,
    BackboneConfig,
    BackboneSection,
    GitHubConfig,
    RoutingConfig,
)
from agent_backbone.models import (
    CommentData,
    EventType,
    IssueData,
    IssueEvent,
    ParsedLabels,
)
from agent_backbone.services.database import BackboneDB

_TEST_GITHUB_APP_KEY = Path(__file__).parent / "fixtures" / "github-app-test-key.pem"

TEST_API_KEY = "test-api-key-123"
TEST_REPO = "example/orchestration"

# Agent names used throughout the suite. They are arbitrary test data.
AGENT_NAMES = ("feynman", "ike", "leo", "ada", "brunel", "hamilton", "curie", "bell", "gallup")


def make_agents(tmp_path: Path | None = None, names=AGENT_NAMES) -> AgentsConfig:
    base = str(tmp_path) if tmp_path else "~/agents"
    return AgentsConfig(
        specs={name: AgentSpec(name=name, dir=f"{base}/{name}", runtime="claude") for name in names}
    )


def make_config(tmp_path: Path, **overrides) -> BackboneConfig:
    """Build a test config with agents and a temp data dir."""
    defaults = dict(
        api_key=TEST_API_KEY,
        webhook_secret="test-secret",
        github_token="ghp_test",
        backbone=BackboneSection(data_dir=str(tmp_path / "data"), port=7120),
        agents=make_agents(tmp_path),
        github=GitHubConfig(repo=TEST_REPO),
        routing=RoutingConfig(ignore_targets=frozenset({"elias"})),
    )
    defaults.update(overrides)
    config = BackboneConfig(**defaults)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level caches between tests."""
    from agent_backbone.api.session_updates import reset_sessions_update_state
    from agent_backbone.services.routing import _dedup

    yield
    _dedup.clear()
    reset_sessions_update_state()


@pytest.fixture
def config(tmp_path):
    """Test BackboneConfig with agents and a temp data dir."""
    return make_config(tmp_path)


@pytest.fixture
def sample_labels():
    return ParsedLabels(sender="leo", targets=["ike"], issue_type="task", priority="")


@pytest.fixture
def sample_issue(sample_labels):
    return IssueData(
        number=42,
        title="[task] Update config",
        state="open",
        labels=sample_labels,
        html_url=f"https://github.com/{TEST_REPO}/issues/42",
        repo_full_name=TEST_REPO,
    )


@pytest.fixture
def sample_comment():
    return CommentData(body="This is a test comment", user_login="someone")


@pytest.fixture
def sample_issue_event(sample_issue):
    return IssueEvent(
        event_type=EventType.ISSUE_OPENED,
        issue=sample_issue,
        delivery_id="test-delivery-1",
    )


@pytest.fixture
def sample_comment_event(sample_issue, sample_comment):
    return IssueEvent(
        event_type=EventType.COMMENT_CREATED,
        issue=sample_issue,
        comment=sample_comment,
        delivery_id="test-delivery-2",
    )


@pytest.fixture
def sample_close_event():
    labels = ParsedLabels(sender="ike", targets=["feynman"], issue_type="task")
    issue = IssueData(
        number=10,
        title="[task] Fix something",
        state="closed",
        labels=labels,
        repo_full_name=TEST_REPO,
    )
    return IssueEvent(event_type=EventType.ISSUE_CLOSED, issue=issue)


@pytest.fixture
def mock_tmux():
    """Mock tmux operations behind the TmuxService facade."""
    with (
        patch(
            "agent_backbone.services.terminal.interface._session_exists", new_callable=AsyncMock
        ) as mock_exists,
        patch(
            "agent_backbone.services.terminal.interface._list_sessions", new_callable=AsyncMock
        ) as mock_list,
    ):
        mock_exists.return_value = True
        mock_list.return_value = list(AGENT_NAMES)
        yield {"session_exists": mock_exists, "list_sessions": mock_list}


@pytest.fixture
def github_issue_json():
    return {
        "number": 42,
        "title": "[task] Update config",
        "state": "open",
        "html_url": f"https://github.com/{TEST_REPO}/issues/42",
        "labels": [{"name": "from:leo"}, {"name": "for:ike"}, {"name": "task"}],
    }


@pytest.fixture
def webhook_payload(github_issue_json):
    return {
        "action": "opened",
        "issue": github_issue_json,
        "repository": {"full_name": TEST_REPO},
    }


@pytest.fixture
async def api_app(config):
    """FastAPI app with test config and in-memory DB (lifespan is not run).

    create_app() returns a socketio.ASGIApp wrapping FastAPI; tests need the
    inner FastAPI app for dependency_overrides and state.
    """
    from agent_backbone.api.app import create_app
    from agent_backbone.base import LifecycleManager
    from agent_backbone.services.agents.interface import StateService
    from agent_backbone.services.database import build_engine
    from agent_backbone.services.routing import DeliveryService, DispatchService
    from agent_backbone.services.terminal import TmuxService

    asgi_app = create_app(config)
    app = asgi_app.other_asgi_app

    app.state.lifecycle = LifecycleManager()
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    db = BackboneDB(engine)
    await db.start()
    app.state.db = db
    app.state.github = None  # Tests override via dependency_overrides when needed
    app.state.state_service = StateService(config.state_dir, db=db)
    app.state.tmux_service = TmuxService()
    app.state.delivery_service = DeliveryService()
    app.state.dispatch_service = DispatchService()
    app.state.telegram_service = None
    app.state.scheduler = None

    yield app

    db._engine = None
    await engine.dispose()


@pytest.fixture
async def api_client(api_app):
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def api_key():
    return TEST_API_KEY


@pytest.fixture
def auth_headers(api_key):
    return {"Authorization": f"Bearer {api_key}"}
