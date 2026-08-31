"""Tests for the plan endpoints at api/routes/plans.py."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.api.deps import get_state_service, get_tmux_service
from agent_backbone.config import SecurityConfig
from agent_backbone.services.agents import AgentState, StateSnapshot


def _plan_snapshot(plan_file: str | None = None) -> StateSnapshot:
    return StateSnapshot(
        state=AgentState.PLAN_WAITING, source="push", plan_file=plan_file, plan_title="Plan"
    )


@pytest.fixture
def state_svc():
    svc = MagicMock()
    svc.get_state = AsyncMock(return_value=StateSnapshot(state=AgentState.IDLE))
    svc.read_state = MagicMock(return_value=None)
    return svc


@pytest.fixture
def tmux_svc():
    svc = MagicMock()
    svc.list_sessions = AsyncMock(return_value=["ike"])
    svc.session_exists = AsyncMock(return_value=True)
    svc.send_keys = AsyncMock(return_value=True)
    return svc


@pytest.fixture(autouse=True)
def _override(api_app, state_svc, tmux_svc):
    api_app.dependency_overrides[get_state_service] = lambda: state_svc
    api_app.dependency_overrides[get_tmux_service] = lambda: tmux_svc
    yield
    api_app.dependency_overrides.clear()


def _enable_plan_control(api_app):
    api_app.state.config = replace(
        api_app.state.config, security=SecurityConfig(allow_remote_plan_control=True)
    )


class TestListPlans:
    async def test_lists_plan_waiting_agents(self, api_client, auth_headers, state_svc):
        async def _state(session):
            return _plan_snapshot("/p.md") if session == "ike" else StateSnapshot(AgentState.IDLE)

        state_svc.get_state = AsyncMock(side_effect=_state)
        resp = await api_client.get("/api/plans", headers=auth_headers)
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["session"] == "ike"


class TestGetPlan:
    async def test_returns_plan_content(self, api_client, auth_headers, state_svc, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text("# The plan")
        state_svc.read_state.return_value = _plan_snapshot(str(plan))
        resp = await api_client.get("/api/plans/ike", headers=auth_headers)
        assert resp.json()["content"] == "# The plan"

    async def test_404_when_not_waiting(self, api_client, auth_headers):
        resp = await api_client.get("/api/plans/ike", headers=auth_headers)
        assert resp.status_code == 404


class TestPlanControl:
    async def test_approve_disabled_by_default(self, api_client, auth_headers, tmux_svc):
        resp = await api_client.post("/api/plans/ike/approve", headers=auth_headers)
        assert resp.status_code == 403
        tmux_svc.send_keys.assert_not_awaited()

    async def test_approve_sends_shift_tab_when_enabled(
        self, api_client, auth_headers, api_app, tmux_svc
    ):
        _enable_plan_control(api_app)
        resp = await api_client.post("/api/plans/ike/approve", headers=auth_headers)
        assert resp.json()["action"] == "plan_approved"
        assert [c.args[1] for c in tmux_svc.send_keys.await_args_list] == ["Escape", "[Z"]

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
