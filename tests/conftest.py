"""Shared test fixtures for agent-backbone."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agent_backbone.config import (
    AgentsConfig,
    AgentSpec,
    BackboneConfig,
    BackboneSection,
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


def make_agents(
    tmp_path: Path | None = None, names=AGENT_NAMES, *, shared_repo: str = TEST_REPO
) -> AgentsConfig:
    """Agents that each own ``example/<name>`` and all watch the shared test repo.

    ``for:<agent>`` labels in the shared repo therefore route to every agent,
    while unlabelled issues in ``example/<name>`` belong to that agent alone.
    """
    base = str(tmp_path) if tmp_path else "~/agents"
    if tmp_path is not None:
        for name in names:
            (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return AgentsConfig(
        specs={
            name: AgentSpec(
                name=name,
                dir=f"{base}/{name}",
                runtime="claude",
                repo=f"example/{name}",
                watches=(shared_repo,) if shared_repo else (),
            )
            for name in names
        }
    )


def make_config(tmp_path: Path, **overrides) -> BackboneConfig:
    """Build a test config with agents and a temp data dir."""
    defaults = dict(
        api_key=TEST_API_KEY,
        webhook_secret="test-secret",
        github_token="ghp_test",
        backbone=BackboneSection(data_dir=str(tmp_path / "data"), port=7120),
        agents=make_agents(tmp_path),
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
    from agent_backbone.services.routing import _dedup

    yield
    _dedup.clear()


@pytest.fixture(autouse=True)
def no_real_tmux():
    """The suite never touches the developer's tmux server.

    Every tmux call goes through one subprocess entry point; unless a test
    patches it (or a function above it) deliberately, reaching it is a bug
    in the test's mocking, not something to run.
    """

    async def _refuse(*args, **kwargs):
        raise RuntimeError(f"unpatched tmux call in a test: {' '.join(map(str, args))}")

    with patch("agent_backbone.services.terminal._core.asyncio.create_subprocess_exec", _refuse):
        yield


@pytest.fixture
async def db():
    """An in-memory BackboneDB with the full schema, disposed after the test."""
    async with BackboneDB.connect() as db:
        yield db


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
    from agent_backbone.api.session_updates import SessionFeed
    from agent_backbone.base import LifecycleManager

    asgi_app = create_app(config)
    app = asgi_app.other_asgi_app

    app.state.lifecycle = LifecycleManager()
    db = BackboneDB("sqlite+aiosqlite:///:memory:")
    await db.start()
    app.state.db = db
    app.state.github = None  # Tests override via dependency_overrides when needed
    from dataclasses import replace

    from agent_backbone.services.agents import AgentStore

    # Seed the database with the test agents so store refreshes reproduce them,
    # and keep the test config's secrets/sections when the store publishes.
    for spec in config.agents:
        await db.agents.upsert(
            spec.name,
            dir=spec.dir,
            runtime=spec.runtime,
            model=spec.model,
            repo=spec.repo,
            tags=list(spec.tags),
            env=dict(spec.env),
            description=spec.description,
            always_on=spec.always_on,
            unattended=spec.unattended,
        )
        for repo in spec.watches:
            await db.agents.add_watch(spec.name, repo)

    def _publish(new_config):
        app.state.config = replace(app.state.config, agents=new_config.agents)

    store = AgentStore(db, config.data_dir, on_change=_publish)
    store._agents = config.agents
    store._config = config
    app.state.agent_store = store
    app.state.feed = SessionFeed(lambda: app.state.config)
    app.state.issue_closed_hooks = ()
    from agent_backbone.services.integrations import build_integrations

    app.state.integrations = build_integrations(lambda: app.state.config, db=db)
    app.state.scheduler = None

    yield app

    await db.stop()


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
