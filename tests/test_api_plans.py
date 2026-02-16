"""Tests for api/routes/plans.py -- plan management endpoints."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

from src.agent_state import AgentState, StateSnapshot


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


# ---------------------------------------------------------------------------
# GET /api/plans
# ---------------------------------------------------------------------------


class TestListPendingPlans:
    async def test_returns_agents_with_plan_waiting(self, api_client, auth_headers):
        """Only agents in plan_waiting state are returned."""
        snapshots = {
            "feynman": _idle_snapshot(),
            "ike": _plan_waiting_snapshot(plan_title="Ike's Plan"),
            "leo": _plan_waiting_snapshot(plan_title="Leo's Plan"),
            "ada": _idle_snapshot(),
            "brunel": _idle_snapshot(),
        }

        async def fake_get_state(_state_dir, session, _threshold):
            return snapshots.get(session, _idle_snapshot())

        with (
            patch("api.routes.plans.get_agent_state", side_effect=fake_get_state),
            patch(
                "api.routes.plans.list_sessions",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            resp = await api_client.get("/api/plans", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        sessions = [p["session"] for p in data["items"]]
        assert "ike" in sessions
        assert "leo" in sessions
        # Idle agents should not appear
        assert "feynman" not in sessions

    async def test_returns_empty_when_no_plans(self, api_client, auth_headers):
        """Returns empty list when no agents have pending plans."""
        with (
            patch(
                "api.routes.plans.get_agent_state",
                new_callable=AsyncMock,
                return_value=_idle_snapshot(),
            ),
            patch(
                "api.routes.plans.list_sessions",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            resp = await api_client.get("/api/plans", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_includes_discovered_coding_agents(self, api_client, auth_headers):
        """Coding agent sessions from tmux are also checked for plan_waiting."""
        plan_snap = _plan_waiting_snapshot(plan_title="Coding Agent Plan")

        async def fake_get_state(_state_dir, session, _threshold):
            if session == "platform-api":
                return plan_snap
            return _idle_snapshot()

        with (
            patch("api.routes.plans.get_agent_state", side_effect=fake_get_state),
            patch(
                "api.routes.plans.list_sessions",
                new_callable=AsyncMock,
                return_value=[
                    "feynman",
                    "ike",
                    "leo",
                    "ada",
                    "brunel",
                    "platform-api",
                ],
            ),
        ):
            resp = await api_client.get("/api/plans", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        sessions = [p["session"] for p in data["items"]]
        assert "platform-api" in sessions

    async def test_requires_auth(self, api_client, api_key):
        """Request without auth headers is rejected when API key is set."""
        resp = await api_client.get("/api/plans")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/plans/{session}
# ---------------------------------------------------------------------------


class TestGetPlanDetail:
    async def test_returns_plan_with_file_content(self, api_client, auth_headers, tmp_path):
        """Returns plan details including file content when plan_file exists."""
        plan_file = tmp_path / "test-plan.md"
        plan_file.write_text("# My Plan\n\nStep 1: Do the thing.\n")

        snapshot = _plan_waiting_snapshot(
            plan_file=str(plan_file),
            plan_title="My Plan",
        )

        with patch("api.routes.plans.read_state_file", return_value=snapshot):
            resp = await api_client.get("/api/plans/ike", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] == "ike"
        assert data["state"] == "plan_waiting"
        assert data["plan_title"] == "My Plan"
        assert data["plan_file"] == str(plan_file)
        assert "# My Plan" in data["content"]
        assert "Step 1" in data["content"]

    async def test_returns_404_when_no_pending_plan(self, api_client, auth_headers):
        """Returns 404 when session has no plan_waiting state."""
        with patch("api.routes.plans.read_state_file", return_value=None):
            resp = await api_client.get("/api/plans/ghost", headers=auth_headers)

        assert resp.status_code == 404
        assert "ghost" in resp.json()["detail"]

    async def test_returns_404_when_state_is_not_plan_waiting(self, api_client, auth_headers):
        """Returns 404 when session exists but state is not plan_waiting."""
        snapshot = _idle_snapshot()

        with patch("api.routes.plans.read_state_file", return_value=snapshot):
            resp = await api_client.get("/api/plans/feynman", headers=auth_headers)

        assert resp.status_code == 404
        assert "feynman" in resp.json()["detail"]

    async def test_content_is_none_when_plan_file_missing(self, api_client, auth_headers):
        """Returns plan detail with content=None when plan_file path does not exist."""
        snapshot = _plan_waiting_snapshot(
            plan_file="/nonexistent/path/plan.md",
            plan_title="Missing File Plan",
        )

        with patch("api.routes.plans.read_state_file", return_value=snapshot):
            resp = await api_client.get("/api/plans/ike", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_title"] == "Missing File Plan"
        assert data["content"] is None

    async def test_content_is_none_when_no_plan_file_set(self, api_client, auth_headers):
        """Returns plan detail with content=None when plan_file is None."""
        snapshot = _plan_waiting_snapshot(plan_file=None, plan_title="No File Plan")

        with patch("api.routes.plans.read_state_file", return_value=snapshot):
            resp = await api_client.get("/api/plans/ike", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_file"] is None
        assert data["content"] is None


# ---------------------------------------------------------------------------
# POST /api/plans/{session}/approve
# ---------------------------------------------------------------------------


class TestApprovePlan:
    async def test_sends_approval_keys_successfully(self, api_client, auth_headers):
        """Sends Escape then [Z (Shift+Tab) to the session and returns ok."""
        with (
            patch("api.routes.plans.session_exists", new_callable=AsyncMock, return_value=True),
            patch(
                "api.routes.plans.send_keys", new_callable=AsyncMock, return_value=True
            ) as mock_keys,
        ):
            resp = await api_client.post("/api/plans/ike/approve", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session"] == "ike"
        assert data["action"] == "plan_approved"
        # Verify the key sequence: Escape first, then [Z
        assert mock_keys.await_count == 2
        mock_keys.assert_any_await("ike", "Escape")
        mock_keys.assert_any_await("ike", "[Z")

    async def test_returns_404_when_session_not_found(self, api_client, auth_headers):
        """Returns 404 when the target session does not exist."""
        with patch("api.routes.plans.session_exists", new_callable=AsyncMock, return_value=False):
            resp = await api_client.post("/api/plans/ghost/approve", headers=auth_headers)

        assert resp.status_code == 404
        assert "ghost" in resp.json()["detail"]

    async def test_returns_500_when_send_keys_fails(self, api_client, auth_headers):
        """Returns 500 when send_keys returns False (key delivery failure)."""
        with (
            patch("api.routes.plans.session_exists", new_callable=AsyncMock, return_value=True),
            patch("api.routes.plans.send_keys", new_callable=AsyncMock, return_value=False),
        ):
            resp = await api_client.post("/api/plans/ike/approve", headers=auth_headers)

        assert resp.status_code == 500
        assert "Failed" in resp.json()["detail"]

    async def test_requires_auth(self, api_client, api_key):
        """Request without auth headers is rejected when API key is set."""
        resp = await api_client.post("/api/plans/ike/approve")
        assert resp.status_code == 401
