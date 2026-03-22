"""Tests for the FastAPI status routes at api/routes/status.py."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

import agent_backbone.api.routes.agents as agents_module
from agent_backbone.api.deps import get_db, get_github, get_state_service, get_tmux_service
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.database import BackboneDB


@pytest.fixture(autouse=True)
def _reset_agents_cache():
    """Reset the TTL cache before each test to prevent cross-test leakage."""
    agents_module._agents_cache = []
    agents_module._agents_cache_ts = 0
    yield
    agents_module._agents_cache = []
    agents_module._agents_cache_ts = 0


def _idle_snapshot() -> StateSnapshot:
    """Return a fresh idle StateSnapshot for test mocking."""
    return StateSnapshot(state=AgentState.IDLE, source="push", timestamp=time.time())


def _make_mock_state_svc(snapshot: StateSnapshot | None = None) -> MagicMock:
    """Create a mock StateService that returns the given snapshot."""
    svc = MagicMock()
    svc.get_state = AsyncMock(return_value=snapshot or _idle_snapshot())
    svc.read_state = MagicMock(return_value=None)
    return svc


def _make_mock_tmux_svc(sessions: list[str] | None = None) -> MagicMock:
    """Create a mock TmuxService that returns the given session list."""
    svc = MagicMock()
    svc.list_sessions = AsyncMock(return_value=sessions or [])
    return svc


@pytest.fixture
async def status_app_with_failures(api_app):
    """App with get_db overridden to use an in-memory DB seeded with a failed delivery."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    db = BackboneDB(engine)
    await db.start()
    await db.record_delivery(
        issue_number=99,
        target_entity="ike",
        session_name="ike",
        outcome="delivery_failed",
        flow_name="test",
    )

    api_app.dependency_overrides[get_db] = lambda: db
    # Also set state and tmux service overrides so tests can customize them
    mock_state_svc = _make_mock_state_svc()
    mock_tmux_svc = _make_mock_tmux_svc()
    api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
    api_app.dependency_overrides[get_tmux_service] = lambda: mock_tmux_svc
    yield api_app
    api_app.dependency_overrides.clear()
    db._engine = None
    await engine.dispose()


@pytest.fixture
async def status_client_with_failures(status_app_with_failures):
    """Async test client bound to the app with seeded failed deliveries."""
    transport = ASGITransport(app=status_app_with_failures)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestGetSystemStatus:
    """Tests for GET /api/status — system digest."""

    async def test_returns_active_sessions_and_agents(
        self, api_client, auth_headers, config, api_app
    ):
        """System status includes active sessions, agent count, and agent list."""
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[object()] * 3)
        mock_state_svc = _make_mock_state_svc()
        mock_tmux_svc = _make_mock_tmux_svc(["feynman", "ike"])

        api_app.dependency_overrides[get_github] = lambda: mock_gh
        api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
        api_app.dependency_overrides[get_tmux_service] = lambda: mock_tmux_svc

        resp = await api_client.get("/api/status", headers=auth_headers)

        del api_app.dependency_overrides[get_github]
        del api_app.dependency_overrides[get_state_service]
        del api_app.dependency_overrides[get_tmux_service]
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_sessions"] == ["feynman", "ike"]
        # Config has 9 named entities
        # (feynman, ike, leo, ada, brunel, hamilton, curie, bell, gallup)
        assert data["agent_count"] == 9
        assert data["pending_issues"] == 3
        assert data["failed_deliveries"] == 0
        assert len(data["agents"]) == 9
        # Named entities get type "named_entity"
        for agent in data["agents"]:
            assert agent["type"] == "named_entity"

    async def test_includes_unnamed_sessions_as_agents(
        self, api_client, auth_headers, config, api_app
    ):
        """Sessions not in config.registry.sessions_map are included as extra agents."""
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[])
        mock_state_svc = _make_mock_state_svc()
        # "platform-api" is not a named entity session
        mock_tmux_svc = _make_mock_tmux_svc(["feynman", "ike", "platform-api"])

        api_app.dependency_overrides[get_github] = lambda: mock_gh
        api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
        api_app.dependency_overrides[get_tmux_service] = lambda: mock_tmux_svc

        resp = await api_client.get("/api/status", headers=auth_headers)

        del api_app.dependency_overrides[get_github]
        del api_app.dependency_overrides[get_state_service]
        del api_app.dependency_overrides[get_tmux_service]
        assert resp.status_code == 200
        data = resp.json()
        # 9 named entities + 1 unnamed session (platform-api)
        assert data["agent_count"] == 10
        sessions_in_agents = [a["session"] for a in data["agents"]]
        assert "platform-api" in sessions_in_agents
        # Verify type classification
        coding = next(a for a in data["agents"] if a["session"] == "platform-api")
        assert coding["type"] == "coding_agent"
        named = next(a for a in data["agents"] if a["session"] == "feynman")
        assert named["type"] == "named_entity"

    async def test_excludes_service_sessions(self, api_client, auth_headers, config, api_app):
        """Service sessions (gateway, telegram-bot) are excluded from status digest."""
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[])
        mock_state_svc = _make_mock_state_svc()
        mock_tmux_svc = _make_mock_tmux_svc(
            [
                "feynman",
                "ike",
                "platform-api",
                "telegram-bot",
            ]
        )

        api_app.dependency_overrides[get_github] = lambda: mock_gh
        api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
        api_app.dependency_overrides[get_tmux_service] = lambda: mock_tmux_svc

        resp = await api_client.get("/api/status", headers=auth_headers)

        del api_app.dependency_overrides[get_github]
        del api_app.dependency_overrides[get_state_service]
        del api_app.dependency_overrides[get_tmux_service]
        assert resp.status_code == 200
        data = resp.json()
        sessions = [a["session"] for a in data["agents"]]
        assert "platform-api" in sessions
        assert "telegram-bot" not in sessions
        # 9 named + 1 coding agent (platform-api), no services
        assert data["agent_count"] == 10

    async def test_counts_failed_deliveries(
        self, status_client_with_failures, auth_headers, status_app_with_failures
    ):
        """Failed delivery count reflects records in the database."""
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[])
        status_app_with_failures.dependency_overrides[get_github] = lambda: mock_gh
        # Update tmux to return empty sessions for this test
        mock_tmux_svc = _make_mock_tmux_svc([])
        status_app_with_failures.dependency_overrides[get_tmux_service] = lambda: mock_tmux_svc

        resp = await status_client_with_failures.get("/api/status", headers=auth_headers)

        del status_app_with_failures.dependency_overrides[get_github]
        assert resp.status_code == 200
        data = resp.json()
        assert data["failed_deliveries"] == 1


class TestGetServiceHealth:
    """Tests for GET /api/status/services — service health checks."""

    async def test_all_services_up(self, api_client, auth_headers):
        """When DB connection works, gateway and database report up."""
        resp = await api_client.get(
            "/api/status/services", headers=auth_headers
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["gateway"] == "up"
        assert data["database"] == "up"

    async def test_database_down(self, api_client, auth_headers, api_app):
        """When DB connection fails, database reports down."""
        mock_db = AsyncMock()
        mock_db.check_connection = AsyncMock(return_value=False)
        api_app.dependency_overrides[get_db] = lambda: mock_db
        try:
            resp = await api_client.get(
                "/api/status/services", headers=auth_headers
            )
        finally:
            del api_app.dependency_overrides[get_db]

        assert resp.status_code == 200
        data = resp.json()
        assert data["gateway"] == "up"
        assert data["database"] == "down"


class TestGetEntityConfig:
    """Tests for GET /api/config/entities — entity configuration."""

    async def test_returns_entity_config(self, api_client, auth_headers, config):
        """Entity config endpoint returns sessions, coding_repos, skip, and fallback."""
        resp = await api_client.get("/api/config/entities", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert "coding_repos" in data
        assert "skip" in data
        assert "fallback" in data
        # Sessions from the default config
        assert data["sessions"]["feynman"] == "feynman"
        assert data["sessions"]["ike"] == "ike"
        # Lists are sorted
        assert data["coding_repos"] == sorted(data["coding_repos"])
        assert data["skip"] == sorted(data["skip"])

    async def test_requires_auth(self, api_client, api_key):
        """Status routes require authentication when API key is set."""
        resp = await api_client.get("/api/config/entities")
        assert resp.status_code == 401
