"""Tests for the plan endpoints at api/routes/plans.py."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.config import SecurityConfig
from agent_backbone.services.agents import AgentState, StateSnapshot

_PLANS = "agent_backbone.api.routes.plans"


def _plan_snapshot(plan_file: str | None = None) -> StateSnapshot:
    return StateSnapshot(
        state=AgentState.WAITING_FOR_HUMAN,
        reason="plan",
        source="push",
        plan_file=plan_file,
        plan_title="Plan",
    )


@pytest.fixture(autouse=True)
def state_svc():
    """The two state reads the plan routes make: reconciled, and the raw hook file."""
    mocks = SimpleNamespace(
        get_state=AsyncMock(return_value=StateSnapshot(state=AgentState.IDLE)),
        read_state=MagicMock(return_value=None),
    )
    with (
        patch(f"{_PLANS}.agent_state", mocks.get_state),
        patch(f"{_PLANS}.read_state_file", mocks.read_state),
    ):
        yield mocks


@pytest.fixture(autouse=True)
def tmux_svc():
    mocks = SimpleNamespace(
        list_sessions=AsyncMock(return_value=["ike"]),
        session_exists=AsyncMock(return_value=True),
        send_keys=AsyncMock(return_value=True),
    )
    with (
        patch(f"{_PLANS}.list_sessions", mocks.list_sessions),
        patch(f"{_PLANS}.session_exists", mocks.session_exists),
        patch(f"{_PLANS}.send_keys", mocks.send_keys),
    ):
        yield mocks


def _enable_plan_control(api_app):
    api_app.state.config = replace(
        api_app.state.config, security=SecurityConfig(allow_remote_plan_control=True)
    )


class TestListPlans:
    async def test_lists_plan_waiting_agents(self, api_client, auth_headers, state_svc):
        async def _state(config, session):
            return _plan_snapshot("/p.md") if session == "ike" else StateSnapshot(AgentState.IDLE)

        state_svc.get_state.side_effect = _state
        resp = await api_client.get("/api/plans", headers=auth_headers)
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["session"] == "ike"


class TestGetPlan:
    async def test_returns_plan_content(self, api_client, auth_headers, api_app, state_svc):
        plans_dir = api_app.state.config.state_dir / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan = plans_dir / "ike.md"
        plan.write_text("# The plan")
        state_svc.read_state.return_value = _plan_snapshot(str(plan))
        resp = await api_client.get("/api/plans/ike", headers=auth_headers)
        assert resp.json()["content"] == "# The plan"

    async def test_plan_file_outside_plans_dir_is_not_read(
        self, api_client, auth_headers, state_svc, tmp_path
    ):
        # The recorded path is data from a state file / POST body: a path
        # anywhere else on the machine must not be served back.
        secret = tmp_path / "secret.txt"
        secret.write_text("hunter2")
        state_svc.read_state.return_value = _plan_snapshot(str(secret))
        resp = await api_client.get("/api/plans/ike", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["content"] is None
        assert resp.json()["plan_file"] == str(secret)

    async def test_plan_file_traversal_is_not_read(
        self, api_client, auth_headers, api_app, state_svc, tmp_path
    ):
        plans_dir = api_app.state.config.state_dir / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        sneaky = str(plans_dir / ".." / ".." / ".." / "outside.md")
        outside = Path(sneaky).resolve()
        outside.write_text("nope")  # the traversal target really exists...
        assert not outside.is_relative_to(plans_dir.resolve())  # ...and is outside
        state_svc.read_state.return_value = _plan_snapshot(sneaky)
        resp = await api_client.get("/api/plans/ike", headers=auth_headers)
        assert resp.json()["content"] is None

    async def test_404_when_not_waiting(self, api_client, auth_headers):
        resp = await api_client.get("/api/plans/ike", headers=auth_headers)
        assert resp.status_code == 404


class TestPlanControl:
    async def test_approve_disabled_by_default(self, api_client, auth_headers):
        with patch(f"{_PLANS}.approve_plan", new_callable=AsyncMock) as approve:
            resp = await api_client.post("/api/plans/ike/approve", headers=auth_headers)
        assert resp.status_code == 403
        approve.assert_not_awaited()

    async def test_plan_control_only_targets_registered_agents(
        self, api_client, auth_headers, api_app, tmux_svc
    ):
        # "stray" is a live tmux session but not a backbone agent: no keys.
        _enable_plan_control(api_app)
        resp = await api_client.post("/api/plans/stray/approve", headers=auth_headers)
        assert resp.status_code == 404
        assert "not a registered agent" in resp.json()["detail"]
        resp = await api_client.post(
            "/api/plans/stray/respond", json={"input": "1"}, headers=auth_headers
        )
        assert resp.status_code == 404
        tmux_svc.send_keys.assert_not_awaited()

    async def test_approve_sends_shift_tab_when_enabled(self, api_client, auth_headers, api_app):
        _enable_plan_control(api_app)
        with patch(f"{_PLANS}.approve_plan", new_callable=AsyncMock, return_value=True) as approve:
            resp = await api_client.post("/api/plans/ike/approve", headers=auth_headers)
        assert resp.json()["action"] == "plan_approved"
        approve.assert_awaited_once_with("ike")

    async def test_reject_delivers_feedback(self, api_client, auth_headers, api_app, state_svc):
        _enable_plan_control(api_app)
        state_svc.read_state.return_value = _plan_snapshot()
        with patch(
            "agent_backbone.api.routes.plans.send_message",
            new_callable=AsyncMock,
            return_value=True,
        ) as send:
            resp = await api_client.post(
                "/api/plans/ike/reject", json={"feedback": "too big"}, headers=auth_headers
            )
        assert resp.json()["action"] == "plan_rejected"
        assert "too big" in send.await_args.args[1]

    async def test_reject_requires_plan_waiting(self, api_client, auth_headers, api_app):
        _enable_plan_control(api_app)
        resp = await api_client.post(
            "/api/plans/ike/reject", json={"feedback": "x"}, headers=auth_headers
        )
        assert resp.status_code == 409

    async def test_respond_sends_input(self, api_client, auth_headers, api_app, state_svc):
        _enable_plan_control(api_app)
        state_svc.read_state.return_value = _plan_snapshot()
        with patch(
            "agent_backbone.api.routes.plans.send_message",
            new_callable=AsyncMock,
            return_value=True,
        ) as send:
            resp = await api_client.post(
                "/api/plans/ike/respond", json={"input": "2"}, headers=auth_headers
            )
        assert resp.json()["action"] == "plan_response_sent"
        send.assert_awaited_once_with("ike", "2")
