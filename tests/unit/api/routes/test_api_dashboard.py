"""Tests for api/routes/dashboard.py — unified dashboard endpoint."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent_backbone.api.routes.dashboard as dashboard_module
import agent_backbone.api.session_updates as session_updates_module
from agent_backbone.api.deps import get_db, get_github, get_state_service, get_tmux_service
from agent_backbone.api.models import EnrichedAgent
from agent_backbone.services.agents import AgentState, StateSnapshot


@pytest.fixture(autouse=True)
def _reset_dashboard_caches():
    """Reset dashboard-local caches and the shared session snapshot cache."""
    session_updates_module.reset_sessions_update_state()
    dashboard_module._issues_cache = 0
    dashboard_module._issues_cache_ts = 0
    yield
    session_updates_module.reset_sessions_update_state()
    dashboard_module._issues_cache = 0
    dashboard_module._issues_cache_ts = 0


def _snapshot(state: AgentState = AgentState.IDLE, **kw) -> StateSnapshot:
    defaults = dict(state=state, source="push", timestamp=time.time())
    defaults.update(kw)
    return StateSnapshot(**defaults)


def _make_state_svc(snapshot: StateSnapshot | None = None) -> MagicMock:
    svc = MagicMock()
    svc.get_state = AsyncMock(return_value=snapshot or _snapshot())
    svc.get_reported_state = AsyncMock(return_value=snapshot or _snapshot())
    return svc


def _make_tmux_svc(rich_sessions: list | None = None) -> MagicMock:
    svc = MagicMock()
    svc.list_sessions_rich = AsyncMock(return_value=rich_sessions or [])
    return svc


def _make_gh(issue_count: int = 3) -> MagicMock:
    gh = MagicMock()
    gh.list_issues = AsyncMock(return_value=[MagicMock() for _ in range(issue_count)])
    return gh


def _make_db(failed_count: int = 0, connection_ok: bool = True) -> MagicMock:
    db = MagicMock()
    db.get_failed_deliveries = AsyncMock(return_value=[{} for _ in range(failed_count)])
    db.check_connection = AsyncMock(return_value=connection_ok)
    return db


def _set_overrides(api_app, *, state_svc=None, tmux_svc=None, gh=None, db=None):
    if state_svc is not None:
        api_app.dependency_overrides[get_state_service] = lambda: state_svc
    if tmux_svc is not None:
        api_app.dependency_overrides[get_tmux_service] = lambda: tmux_svc
    if gh is not None:
        api_app.dependency_overrides[get_github] = lambda: gh
    if db is not None:
        api_app.dependency_overrides[get_db] = lambda: db


def _clear_overrides(api_app):
    api_app.dependency_overrides.pop(get_state_service, None)
    api_app.dependency_overrides.pop(get_tmux_service, None)
    api_app.dependency_overrides.pop(get_github, None)
    api_app.dependency_overrides.pop(get_db, None)


class TestDashboardEndpoint:
    async def test_returns_all_agents(self, api_app, api_client, auth_headers):
        """Agent list is populated with named entities."""
        _set_overrides(
            api_app,
            state_svc=_make_state_svc(),
            tmux_svc=_make_tmux_svc(),
            gh=_make_gh(issue_count=0),
            db=_make_db(),
        )
        try:
            resp = await api_client.get("/api/dashboard", headers=auth_headers)
        finally:
            _clear_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["agents"]) > 0
        sessions = [a["session"] for a in data["agents"]]
        assert "ike" in sessions
        assert "feynman" in sessions

    async def test_counts_computed(self, api_app, api_client, auth_headers):
        """Counts reflect agent states — idle agents are counted as active."""
        _set_overrides(
            api_app,
            state_svc=_make_state_svc(),
            tmux_svc=_make_tmux_svc(
                rich_sessions=[
                    {"name": "ike", "windows": 1, "created": 1708000000, "attached": False},
                    {"name": "feynman", "windows": 1, "created": 1708000000, "attached": False},
                ]
            ),
            gh=_make_gh(issue_count=0),
            db=_make_db(),
        )
        try:
            resp = await api_client.get("/api/dashboard", headers=auth_headers)
        finally:
            _clear_overrides(api_app)

        assert resp.status_code == 200
        counts = resp.json()["counts"]
        assert counts["total"] > 0
        # Agents with idle state and online=True are idle+active
        # Agents offline are counted as offline
        non_total = counts["idle"] + counts["busy"] + counts["plan_waiting"] + counts["offline"]
        assert non_total <= counts["total"]

    async def test_plans_pending_count(self, api_app, api_client, auth_headers):
        """Agents with plan_waiting state are counted in plans_pending."""
        plan_snapshot = _snapshot(
            state=AgentState.PLAN_WAITING,
            plan_file="/tmp/plan.md",
            plan_title="My Plan",
        )
        _set_overrides(
            api_app,
            state_svc=_make_state_svc(plan_snapshot),
            tmux_svc=_make_tmux_svc(
                rich_sessions=[
                    {"name": "ike", "windows": 1, "created": 1708000000, "attached": False},
                ]
            ),
            gh=_make_gh(issue_count=0),
            db=_make_db(),
        )
        try:
            resp = await api_client.get("/api/dashboard", headers=auth_headers)
        finally:
            _clear_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        # At least 1 agent has plan_waiting state (ike is online + plan_waiting)
        assert data["plans_pending"] >= 1
        assert data["counts"]["plan_waiting"] >= 1

    async def test_issues_pending_from_github(self, api_app, api_client, auth_headers):
        """Issues pending count comes from GitHub API."""
        _set_overrides(
            api_app,
            state_svc=_make_state_svc(),
            tmux_svc=_make_tmux_svc(),
            gh=_make_gh(issue_count=7),
            db=_make_db(),
        )
        try:
            resp = await api_client.get("/api/dashboard", headers=auth_headers)
        finally:
            _clear_overrides(api_app)

        assert resp.status_code == 200
        assert resp.json()["issues_pending"] == 7

    async def test_failed_deliveries_from_db(self, api_app, api_client, auth_headers):
        """Failed deliveries count comes from the DB."""
        _set_overrides(
            api_app,
            state_svc=_make_state_svc(),
            tmux_svc=_make_tmux_svc(),
            gh=_make_gh(issue_count=0),
            db=_make_db(failed_count=4),
        )
        try:
            resp = await api_client.get("/api/dashboard", headers=auth_headers)
        finally:
            _clear_overrides(api_app)

        assert resp.status_code == 200
        assert resp.json()["failed_deliveries"] == 4

    async def test_service_health_included(self, api_app, api_client, auth_headers):
        """Services field includes gateway and database health."""
        _set_overrides(
            api_app,
            state_svc=_make_state_svc(),
            tmux_svc=_make_tmux_svc(),
            gh=_make_gh(issue_count=0),
            db=_make_db(connection_ok=True),
        )
        try:
            resp = await api_client.get("/api/dashboard", headers=auth_headers)
        finally:
            _clear_overrides(api_app)

        assert resp.status_code == 200
        services = resp.json()["services"]
        assert services["gateway"] == "up"
        assert services["database"] == "up"

    async def test_cache_returns_same_within_ttl(self, api_app, api_client, auth_headers):
        """Two rapid calls return same data (cache hit)."""
        gh = _make_gh(issue_count=5)
        _set_overrides(
            api_app,
            state_svc=_make_state_svc(),
            tmux_svc=_make_tmux_svc(),
            gh=gh,
            db=_make_db(),
        )
        try:
            resp1 = await api_client.get("/api/dashboard", headers=auth_headers)
            # Change the mock — if cache works, the second call still returns 5
            gh.list_issues = AsyncMock(return_value=[MagicMock() for _ in range(99)])
            resp2 = await api_client.get("/api/dashboard", headers=auth_headers)
        finally:
            _clear_overrides(api_app)

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # Issue count should be cached (5, not 99)
        assert resp2.json()["issues_pending"] == 5

    async def test_reuses_shared_agents_snapshot_from_agents_endpoint(
        self, api_app, api_client, auth_headers
    ):
        """Dashboard reuses the shared snapshot populated by /api/agents."""
        shared_snapshot = [
            EnrichedAgent(
                session="ike",
                entity="ike",
                state="idle",
                online=True,
                type="named_entity",
            )
        ]
        _set_overrides(
            api_app,
            state_svc=_make_state_svc(),
            tmux_svc=_make_tmux_svc(),
            gh=_make_gh(issue_count=0),
            db=_make_db(),
        )
        try:
            with (
                patch(
                    "agent_backbone.api.routes.agents.build_session_snapshot",
                    new_callable=AsyncMock,
                    return_value=shared_snapshot,
                ) as mock_agents_build,
                patch(
                    "agent_backbone.api.routes.dashboard.build_session_snapshot",
                    new_callable=AsyncMock,
                ) as mock_dashboard_build,
            ):
                agents_resp = await api_client.get("/api/agents", headers=auth_headers)
                dashboard_resp = await api_client.get("/api/dashboard", headers=auth_headers)
        finally:
            _clear_overrides(api_app)

        assert agents_resp.status_code == 200
        assert dashboard_resp.status_code == 200
        assert mock_agents_build.await_count == 1
        mock_dashboard_build.assert_not_awaited()
        assert dashboard_resp.json()["agents"][0]["session"] == "ike"

    async def test_state_since_populated(self, api_app, api_client, auth_headers):
        """state_since field is populated on agents from snapshot timestamp."""
        ts = 1708617600.0
        _set_overrides(
            api_app,
            state_svc=_make_state_svc(_snapshot(timestamp=ts)),
            tmux_svc=_make_tmux_svc(
                rich_sessions=[
                    {"name": "ike", "windows": 1, "created": 1708000000, "attached": False},
                ]
            ),
            gh=_make_gh(issue_count=0),
            db=_make_db(),
        )
        try:
            resp = await api_client.get("/api/dashboard", headers=auth_headers)
        finally:
            _clear_overrides(api_app)

        assert resp.status_code == 200
        ike = next(a for a in resp.json()["agents"] if a["session"] == "ike")
        assert ike["state_since"] == ts
