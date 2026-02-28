"""Tests for the FastAPI status routes at api/routes/status.py."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import api.routes.agents as agents_module
from agent_backbone.services.persistence import BackboneDB
from agent_backbone.services.state import AgentState, StateSnapshot
from api.deps import get_db, get_github


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


@pytest.fixture
async def status_app_with_failures(api_app):
    """App with get_db overridden to use an in-memory DB seeded with a failed delivery."""
    db = BackboneDB("sqlite+aiosqlite:///:memory:")
    await db.start()
    await db.record_delivery(
        issue_number=99,
        target_entity="ike",
        session_name="ike",
        outcome="delivery_failed",
        flow_name="test",
    )

    api_app.dependency_overrides[get_db] = lambda: db
    yield api_app
    api_app.dependency_overrides.clear()
    await db.stop()


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
        snapshot = _idle_snapshot()
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[object()] * 3)
        api_app.dependency_overrides[get_github] = lambda: mock_gh

        with (
            patch("api.routes.status.list_sessions", new_callable=AsyncMock) as mock_list,
            patch("api.routes.agents.get_agent_state", new_callable=AsyncMock) as mock_state,
        ):
            mock_list.return_value = ["feynman", "ike"]
            mock_state.return_value = snapshot

            resp = await api_client.get("/api/status", headers=auth_headers)

        del api_app.dependency_overrides[get_github]
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
        snapshot = _idle_snapshot()
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[])
        api_app.dependency_overrides[get_github] = lambda: mock_gh

        with (
            patch("api.routes.status.list_sessions", new_callable=AsyncMock) as mock_list,
            patch("api.routes.agents.get_agent_state", new_callable=AsyncMock) as mock_state,
        ):
            # "platform-api" is not a named entity session
            mock_list.return_value = ["feynman", "ike", "platform-api"]
            mock_state.return_value = snapshot

            resp = await api_client.get("/api/status", headers=auth_headers)

        del api_app.dependency_overrides[get_github]
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
        """Service sessions (ngrok, prefect-*) are excluded from status digest."""
        snapshot = _idle_snapshot()
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[])
        api_app.dependency_overrides[get_github] = lambda: mock_gh

        with (
            patch("api.routes.status.list_sessions", new_callable=AsyncMock) as mock_list,
            patch("api.routes.agents.get_agent_state", new_callable=AsyncMock) as mock_state,
        ):
            mock_list.return_value = [
                "feynman",
                "ike",
                "platform-api",
                "ngrok",
                "prefect-worker",
                "prefect-server",
                "telegram-bot",
            ]
            mock_state.return_value = snapshot

            resp = await api_client.get("/api/status", headers=auth_headers)

        del api_app.dependency_overrides[get_github]
        assert resp.status_code == 200
        data = resp.json()
        sessions = [a["session"] for a in data["agents"]]
        assert "platform-api" in sessions
        for svc in ("ngrok", "prefect-worker", "prefect-server", "telegram-bot"):
            assert svc not in sessions
        # 9 named + 1 coding agent (platform-api), no services
        assert data["agent_count"] == 10

    async def test_counts_failed_deliveries(
        self, status_client_with_failures, auth_headers, status_app_with_failures
    ):
        """Failed delivery count reflects records in the database."""
        snapshot = _idle_snapshot()
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[])
        status_app_with_failures.dependency_overrides[get_github] = lambda: mock_gh

        with (
            patch("api.routes.status.list_sessions", new_callable=AsyncMock) as mock_list,
            patch("api.routes.agents.get_agent_state", new_callable=AsyncMock) as mock_state,
        ):
            mock_list.return_value = []
            mock_state.return_value = snapshot

            resp = await status_client_with_failures.get("/api/status", headers=auth_headers)

        del status_app_with_failures.dependency_overrides[get_github]
        assert resp.status_code == 200
        data = resp.json()
        assert data["failed_deliveries"] == 1


class TestGetServiceHealth:
    """Tests for GET /api/status/services — service health checks."""

    async def test_all_services_up(self, api_client, auth_headers):
        """When Prefect is reachable and DB works, all services report up."""
        mock_response = httpx.Response(status_code=200, request=httpx.Request("GET", "http://test"))

        with patch("api.routes.status.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            resp = await api_client.get("/api/status/services", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["gateway"] == "up"
        assert data["prefect_server"] == "up"
        assert data["database"] == "up"

    async def test_prefect_down(self, api_client, auth_headers):
        """When Prefect is unreachable, prefect_server reports down."""
        with patch("api.routes.status.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.ConnectError("Connection refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            resp = await api_client.get("/api/status/services", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["gateway"] == "up"
        assert data["prefect_server"] == "down"
        assert data["database"] == "up"

    async def test_prefect_degraded(self, api_client, auth_headers):
        """When Prefect returns non-200, prefect_server reports degraded."""
        mock_response = httpx.Response(status_code=503, request=httpx.Request("GET", "http://test"))

        with patch("api.routes.status.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            resp = await api_client.get("/api/status/services", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["prefect_server"] == "degraded"


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
