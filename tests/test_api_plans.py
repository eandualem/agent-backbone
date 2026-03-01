"""Tests for api/routes/plans.py -- plan management endpoints."""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

from agent_backbone.services.registry import EntityEntry, EntityRegistry
from agent_backbone.services.state import AgentState, StateSnapshot
from api.deps import get_state_service, get_tmux_service


def _plan_waiting_snapshot(**overrides) -> StateSnapshot:
    """Build a plan_waiting StateSnapshot with optional overrides."""
    defaults = dict(
        state=AgentState.PLAN_WAITING,
        source="push",
        timestamp=time.time(),
        plan_file="/tmp/test-plan.md",
        plan_title="Test Plan Title",
    )
    defaults.update(overrides)
    return StateSnapshot(**defaults)


def _idle_snapshot(**overrides) -> StateSnapshot:
    """Build an idle StateSnapshot with optional overrides."""
    defaults = dict(state=AgentState.IDLE, source="push", timestamp=time.time())
    defaults.update(overrides)
    return StateSnapshot(**defaults)


def _make_mock_services(
    *,
    get_state_side_effect=None,
    get_state_return=None,
    read_state_return=None,
    list_sessions_return=None,
    session_exists_return=True,
    send_keys_return=True,
):
    """Build mock state and tmux services with given behaviors."""
    mock_state_svc = MagicMock()
    if get_state_side_effect is not None:
        mock_state_svc.get_state = AsyncMock(side_effect=get_state_side_effect)
    elif get_state_return is not None:
        mock_state_svc.get_state = AsyncMock(return_value=get_state_return)
    else:
        mock_state_svc.get_state = AsyncMock(return_value=_idle_snapshot())

    if read_state_return is not None:
        mock_state_svc.read_state = MagicMock(return_value=read_state_return)
    else:
        mock_state_svc.read_state = MagicMock(return_value=None)

    mock_tmux_svc = MagicMock()
    mock_tmux_svc.list_sessions = AsyncMock(
        return_value=list_sessions_return if list_sessions_return is not None else []
    )
    mock_tmux_svc.session_exists = AsyncMock(return_value=session_exists_return)
    mock_tmux_svc.send_keys = AsyncMock(return_value=send_keys_return)

    return mock_state_svc, mock_tmux_svc


# ---------------------------------------------------------------------------
# GET /api/plans
# ---------------------------------------------------------------------------


class TestListPendingPlans:
    async def test_returns_agents_with_plan_waiting(self, api_client, auth_headers, api_app):
        """Only agents in plan_waiting state are returned."""
        snapshots = {
            "feynman": _idle_snapshot(),
            "ike": _plan_waiting_snapshot(plan_title="Ike's Plan"),
            "leo": _plan_waiting_snapshot(plan_title="Leo's Plan"),
            "ada": _idle_snapshot(),
            "brunel": _idle_snapshot(),
        }

        mock_state_svc, mock_tmux_svc = _make_mock_services(
            get_state_side_effect=lambda session: snapshots.get(session, _idle_snapshot()),
        )

        api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
        api_app.dependency_overrides[get_tmux_service] = lambda: mock_tmux_svc
        try:
            resp = await api_client.get("/api/plans", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_state_service, None)
            api_app.dependency_overrides.pop(get_tmux_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        sessions = [p["session"] for p in data["items"]]
        assert "ike" in sessions
        assert "leo" in sessions
        # Idle agents should not appear
        assert "feynman" not in sessions

    async def test_returns_empty_when_no_plans(self, api_client, auth_headers, api_app):
        """Returns empty list when no agents have pending plans."""
        mock_state_svc, mock_tmux_svc = _make_mock_services(
            get_state_return=_idle_snapshot(),
        )

        api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
        api_app.dependency_overrides[get_tmux_service] = lambda: mock_tmux_svc
        try:
            resp = await api_client.get("/api/plans", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_state_service, None)
            api_app.dependency_overrides.pop(get_tmux_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_includes_discovered_coding_agents(self, api_client, auth_headers, api_app):
        """Coding agent sessions from tmux are also checked for plan_waiting."""
        plan_snap = _plan_waiting_snapshot(plan_title="Coding Agent Plan")

        def fake_get_state(session):
            if session == "platform-api":
                return plan_snap
            return _idle_snapshot()

        mock_state_svc, mock_tmux_svc = _make_mock_services(
            get_state_side_effect=fake_get_state,
            list_sessions_return=[
                "feynman",
                "ike",
                "leo",
                "ada",
                "brunel",
                "platform-api",
            ],
        )

        api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
        api_app.dependency_overrides[get_tmux_service] = lambda: mock_tmux_svc
        try:
            resp = await api_client.get("/api/plans", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_state_service, None)
            api_app.dependency_overrides.pop(get_tmux_service, None)

        assert resp.status_code == 200
        data = resp.json()
        sessions = [p["session"] for p in data["items"]]
        assert "platform-api" in sessions

    async def test_requires_auth(self, api_client, api_key):
        """Request without auth headers is rejected when API key is set."""
        resp = await api_client.get("/api/plans")
        assert resp.status_code == 401

    async def test_handles_none_session_in_registry(self, api_client, auth_headers, api_app):
        """Registry entities with session=None (e.g. Jarvis) don't cause TypeError."""
        # Add a None-session entity to the registry (regression: caused 500 via Path/None)
        config = api_app.state.config
        entities_with_jarvis = dict(config.registry.entities)
        entities_with_jarvis["jarvis"] = EntityEntry(
            session=None,
            home="~/ws/jarvis/",
            groups=[],
            figure="Jarvis",
            role="Personal Assistant",
            entity_type="service",
        )
        patched_registry = EntityRegistry(
            entities=entities_with_jarvis, repos=config.registry.repos
        )
        api_app.state.config = replace(config, registry=patched_registry)

        mock_state_svc, mock_tmux_svc = _make_mock_services(
            get_state_return=_idle_snapshot(),
        )

        api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
        api_app.dependency_overrides[get_tmux_service] = lambda: mock_tmux_svc
        try:
            resp = await api_client.get("/api/plans", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_state_service, None)
            api_app.dependency_overrides.pop(get_tmux_service, None)

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/plans/{session}
# ---------------------------------------------------------------------------


class TestGetPlanDetail:
    async def test_returns_plan_with_file_content(
        self, api_client, auth_headers, api_app, tmp_path
    ):
        """Returns plan details including file content when plan_file exists."""
        plan_file = tmp_path / "test-plan.md"
        plan_file.write_text("# My Plan\n\nStep 1: Do the thing.\n")

        snapshot = _plan_waiting_snapshot(
            plan_file=str(plan_file),
            plan_title="My Plan",
        )

        mock_state_svc, _ = _make_mock_services(read_state_return=snapshot)

        api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
        try:
            resp = await api_client.get("/api/plans/ike", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_state_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] == "ike"
        assert data["state"] == "plan_waiting"
        assert data["plan_title"] == "My Plan"
        assert data["plan_file"] == str(plan_file)
        assert "# My Plan" in data["content"]
        assert "Step 1" in data["content"]

    async def test_returns_404_when_no_pending_plan(self, api_client, auth_headers, api_app):
        """Returns 404 when session has no plan_waiting state."""
        mock_state_svc, _ = _make_mock_services(read_state_return=None)

        api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
        try:
            resp = await api_client.get("/api/plans/ghost", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_state_service, None)

        assert resp.status_code == 404
        assert "ghost" in resp.json()["detail"]

    async def test_returns_404_when_state_is_not_plan_waiting(
        self, api_client, auth_headers, api_app
    ):
        """Returns 404 when session exists but state is not plan_waiting."""
        snapshot = _idle_snapshot()
        mock_state_svc, _ = _make_mock_services(read_state_return=snapshot)

        api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
        try:
            resp = await api_client.get("/api/plans/feynman", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_state_service, None)

        assert resp.status_code == 404
        assert "feynman" in resp.json()["detail"]

    async def test_content_is_none_when_plan_file_missing(self, api_client, auth_headers, api_app):
        """Returns plan detail with content=None when plan_file path does not exist."""
        snapshot = _plan_waiting_snapshot(
            plan_file="/nonexistent/path/plan.md",
            plan_title="Missing File Plan",
        )
        mock_state_svc, _ = _make_mock_services(read_state_return=snapshot)

        api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
        try:
            resp = await api_client.get("/api/plans/ike", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_state_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_title"] == "Missing File Plan"
        assert data["content"] is None

    async def test_content_is_none_when_no_plan_file_set(self, api_client, auth_headers, api_app):
        """Returns plan detail with content=None when plan_file is None."""
        snapshot = _plan_waiting_snapshot(plan_file=None, plan_title="No File Plan")
        mock_state_svc, _ = _make_mock_services(read_state_return=snapshot)

        api_app.dependency_overrides[get_state_service] = lambda: mock_state_svc
        try:
            resp = await api_client.get("/api/plans/ike", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_state_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_file"] is None
        assert data["content"] is None


# ---------------------------------------------------------------------------
# POST /api/plans/{session}/approve
# ---------------------------------------------------------------------------


class TestApprovePlan:
    async def test_sends_approval_keys_successfully(self, api_client, auth_headers, api_app):
        """Sends Escape then [Z (Shift+Tab) to the session and returns ok."""
        _, mock_tmux_svc = _make_mock_services(
            session_exists_return=True,
            send_keys_return=True,
        )

        api_app.dependency_overrides[get_tmux_service] = lambda: mock_tmux_svc
        try:
            resp = await api_client.post("/api/plans/ike/approve", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_tmux_service, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session"] == "ike"
        assert data["action"] == "plan_approved"
        # Verify the key sequence: Escape first, then [Z
        assert mock_tmux_svc.send_keys.await_count == 2
        mock_tmux_svc.send_keys.assert_any_await("ike", "Escape")
        mock_tmux_svc.send_keys.assert_any_await("ike", "[Z")

    async def test_returns_404_when_session_not_found(self, api_client, auth_headers, api_app):
        """Returns 404 when the target session does not exist."""
        _, mock_tmux_svc = _make_mock_services(session_exists_return=False)

        api_app.dependency_overrides[get_tmux_service] = lambda: mock_tmux_svc
        try:
            resp = await api_client.post("/api/plans/ghost/approve", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_tmux_service, None)

        assert resp.status_code == 404
        assert "ghost" in resp.json()["detail"]

    async def test_returns_500_when_send_keys_fails(self, api_client, auth_headers, api_app):
        """Returns 500 when send_keys returns False (key delivery failure)."""
        _, mock_tmux_svc = _make_mock_services(
            session_exists_return=True,
            send_keys_return=False,
        )

        api_app.dependency_overrides[get_tmux_service] = lambda: mock_tmux_svc
        try:
            resp = await api_client.post("/api/plans/ike/approve", headers=auth_headers)
        finally:
            api_app.dependency_overrides.pop(get_tmux_service, None)

        assert resp.status_code == 500
        assert "Failed" in resp.json()["detail"]

    async def test_requires_auth(self, api_client, api_key):
        """Request without auth headers is rejected when API key is set."""
        resp = await api_client.post("/api/plans/ike/approve")
        assert resp.status_code == 401
