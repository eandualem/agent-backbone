"""Tests for the FastAPI status routes at api/routes/status.py."""

from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, patch

from agent_backbone.api.deps import get_optional_github, get_scheduler
from agent_backbone.services.agents import AgentState, StateSnapshot
from agent_backbone.services.scheduler import PeriodicScheduler


def _idle_snapshot() -> StateSnapshot:
    return StateSnapshot(state=AgentState.IDLE, source="push", timestamp=time.time())


@contextlib.contextmanager
def _live(sessions: list[str]):
    """Every agent reads idle; ``sessions`` are the live tmux sessions."""
    with (
        patch(
            "agent_backbone.api.session_updates.agent_state",
            new_callable=AsyncMock,
            return_value=_idle_snapshot(),
        ),
        patch(
            "agent_backbone.api.routes.status.list_sessions",
            new_callable=AsyncMock,
            return_value=sessions,
        ),
    ):
        yield


class TestGetSystemStatus:
    async def test_returns_configured_agents_and_live_sessions(
        self, api_client, auth_headers, api_app, config
    ):
        mock_gh = AsyncMock()
        mock_gh.list_issues = AsyncMock(return_value=[object()] * 3)
        api_app.dependency_overrides[get_optional_github] = lambda: mock_gh

        with _live(["feynman", "ike"]):
            resp = await api_client.get("/api/status", headers=auth_headers)
        api_app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        assert data["active_sessions"] == ["feynman", "ike"]
        assert data["agent_count"] == 9  # nine configured agents in the test config
        assert data["pending_issues"] == 3 * len(config.agents.repos)
        assert data["failed_deliveries"] == 0
        assert {r["repo"] for r in data["repos"]} == set(config.agents.repos)
        shared = next(r for r in data["repos"] if r["repo"] == "example/orchestration")
        assert shared["owners"] == [] and "ike" in shared["watchers"]
        by_name = {a["name"]: a for a in data["agents"]}
        assert by_name["feynman"]["online"] is True
        assert by_name["feynman"]["configured"] is True
        assert by_name["leo"]["online"] is False
        assert by_name["leo"]["state"] == "offline"

    async def test_unconfigured_sessions_are_listed_but_flagged(
        self, api_client, auth_headers, api_app
    ):
        with _live(["ike", "scratch-session", "backbone"]):
            resp = await api_client.get("/api/status", headers=auth_headers)

        data = resp.json()
        names = [a["name"] for a in data["agents"]]
        assert "scratch-session" in names
        assert "backbone" not in names  # the backbone's own session is hidden
        scratch = next(a for a in data["agents"] if a["name"] == "scratch-session")
        assert scratch["configured"] is False
        assert data["pending_issues"] is None  # no GitHub client configured

    async def test_counts_failed_deliveries(self, api_client, auth_headers, api_app):
        await api_app.state.db.deliveries.record(
            issue_number=99,
            target_entity="ike",
            session_name="ike",
            outcome="delivery_failed",
            source="test",
        )
        with _live([]):
            resp = await api_client.get("/api/status", headers=auth_headers)

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
        assert data["integrations"] == {"telegram": "disabled"}
        assert data["github"] == "unconfigured"  # repo set but no client wired in tests


class TestGetAgentConfig:
    async def test_returns_configured_agents(self, api_client, auth_headers, config):
        resp = await api_client.get("/api/config/agents", headers=auth_headers)

        assert resp.status_code == 200
        names = [a["name"] for a in resp.json()]
        assert names == config.agents.names
        assert resp.json()[0]["runtime"] == "claude"
        assert resp.json()[0]["watches"] == ["example/orchestration"]

    async def test_requires_auth(self, api_client):
        resp = await api_client.get("/api/config/agents")
        assert resp.status_code == 401


async def test_upgrade_hold_is_authenticated_scoped_and_refuses_late_requests(
    api_client, auth_headers, api_app
):
    from uuid import uuid4

    from agent_backbone.release import Installation
    from agent_backbone.services.jobs import UpgradeWatch

    watch = UpgradeWatch(
        enabled=lambda: True,
        restart=AsyncMock(),
        in_flight=lambda: 0,
        identity=lambda install: "version:1",
        install=Installation("uv"),
    )
    api_app.state.upgrade_watch = watch
    body = {"operation_id": str(uuid4()), "enabled": True}
    assert (await api_client.post("/api/upgrade/hold", json=body)).status_code in (401, 403)
    held = await api_client.post("/api/upgrade/hold", json=body, headers=auth_headers)
    assert held.json() == {"held": True, "operation_held": True}
    released = await api_client.post(
        "/api/upgrade/hold", json={**body, "enabled": False}, headers=auth_headers
    )
    assert released.json() == {"held": False, "operation_held": False}
    watch.requested = True
    assert (
        await api_client.post("/api/upgrade/hold", json=body, headers=auth_headers)
    ).status_code == 409


async def test_slow_github_does_not_block_local_status(api_client, auth_headers, api_app):
    cancelled = asyncio.Event()

    async def slow(**kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    gh = AsyncMock()
    gh.list_issues.side_effect = slow
    api_app.dependency_overrides[get_optional_github] = lambda: gh
    try:
        with _live(["ike"]):
            response = await asyncio.wait_for(
                api_client.get("/api/status", headers=auth_headers), timeout=4
            )
        assert response.status_code == 200
        assert response.json()["pending_issues"] is None
        assert response.json()["active_sessions"] == ["ike"]
        assert cancelled.is_set()
    finally:
        api_app.dependency_overrides.clear()
