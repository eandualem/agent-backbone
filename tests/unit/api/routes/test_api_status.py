"""Tests for the FastAPI status routes at api/routes/status.py."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

from agent_backbone.api.deps import (
    get_optional_github,
    get_scheduler,
    get_state_service,
    get_tmux_service,
)
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.scheduler import PeriodicScheduler


def _idle_snapshot() -> StateSnapshot:
    return StateSnapshot(state=AgentState.IDLE, source="push", timestamp=time.time())


def _mock_state_svc() -> MagicMock:
    svc = MagicMock()
    svc.get_state = AsyncMock(return_value=_idle_snapshot())
    return svc


def _mock_tmux_svc(sessions: list[str] | None = None) -> MagicMock:
    svc = MagicMock()
    svc.list_sessions = AsyncMock(return_value=sessions or [])
    return svc


class TestGetSystemStatus:
    async def test_returns_configured_agents_and_live_sessions(
        self, api_client, auth_headers, api_app
    ):
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[object()] * 3)
        api_app.dependency_overrides[get_optional_github] = lambda: mock_gh
        api_app.dependency_overrides[get_state_service] = _mock_state_svc
        api_app.dependency_overrides[get_tmux_service] = lambda: _mock_tmux_svc(["feynman", "ike"])

        resp = await api_client.get("/api/status", headers=auth_headers)
        api_app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        assert data["active_sessions"] == ["feynman", "ike"]
        assert data["agent_count"] == 9  # nine configured agents in the test config
        assert data["pending_issues"] == 3
        assert data["failed_deliveries"] == 0
        by_name = {a["name"]: a for a in data["agents"]}
        assert by_name["feynman"]["online"] is True
        assert by_name["feynman"]["configured"] is True
        assert by_name["leo"]["online"] is False
        assert by_name["leo"]["state"] == "offline"

    async def test_unconfigured_sessions_are_listed_but_flagged(
        self, api_client, auth_headers, api_app
    ):
        api_app.dependency_overrides[get_state_service] = _mock_state_svc
        api_app.dependency_overrides[get_tmux_service] = lambda: _mock_tmux_svc(
            ["ike", "scratch-session", "backbone"]
        )

        resp = await api_client.get("/api/status", headers=auth_headers)
        api_app.dependency_overrides.clear()

        data = resp.json()
        names = [a["name"] for a in data["agents"]]
        assert "scratch-session" in names
        assert "backbone" not in names  # the backbone's own session is hidden
        scratch = next(a for a in data["agents"] if a["name"] == "scratch-session")
        assert scratch["configured"] is False
        assert data["pending_issues"] is None  # no GitHub client configured

    async def test_counts_failed_deliveries(self, api_client, auth_headers, api_app):
        await api_app.state.db.record_delivery(99, "ike", "ike", "delivery_failed", "test")
        api_app.dependency_overrides[get_state_service] = _mock_state_svc
        api_app.dependency_overrides[get_tmux_service] = lambda: _mock_tmux_svc([])

        resp = await api_client.get("/api/status", headers=auth_headers)
        api_app.dependency_overrides.clear()

        assert resp.json()["failed_deliveries"] == 1


class TestGetServiceHealth:
    async def test_reports_components(self, api_client, auth_headers, api_app):
        scheduler = PeriodicScheduler()

        async def _noop():
            return None

        scheduler.add("agent-monitor", 60, _noop)
        api_app.dependency_overrides[get_scheduler] = lambda: scheduler

        resp = await api_client.get("/api/status/services", headers=auth_headers)
        api_app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        assert data["api"] == "up"
        assert data["database"] == "up"
        assert data["scheduler"] in ("up", "degraded")
        assert [job["name"] for job in data["jobs"]] == ["agent-monitor"]
        assert data["telegram"] == "disabled"
        assert data["github"] == "unconfigured"  # repo set but no client wired in tests


class TestGetAgentConfig:
    async def test_returns_configured_agents(self, api_client, auth_headers, config):
        resp = await api_client.get("/api/config/agents", headers=auth_headers)

        assert resp.status_code == 200
        names = [a["name"] for a in resp.json()]
        assert names == config.agents.names
        assert resp.json()[0]["runtime"] == "claude"

    async def test_requires_auth(self, api_client):
        resp = await api_client.get("/api/config/agents")
        assert resp.status_code == 401
