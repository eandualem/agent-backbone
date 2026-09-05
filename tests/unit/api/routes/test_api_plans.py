"""Tests for the plan endpoints at api/routes/plans.py."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import SecurityConfig
from agent_backbone.models import DeliveryOutcome
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
    """The one state read the plan routes make: the reconciled state."""
    mocks = SimpleNamespace(
        get_state=AsyncMock(return_value=StateSnapshot(state=AgentState.IDLE)),
    )
    with patch(f"{_PLANS}.agent_state", mocks.get_state):
        yield mocks


@pytest.fixture(autouse=True)
def tmux_svc():
    mocks = SimpleNamespace(list_sessions=AsyncMock(return_value=["ike"]))
    with patch(f"{_PLANS}.list_sessions", mocks.list_sessions):
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
        state_svc.get_state.return_value = _plan_snapshot(str(plan))
        resp = await api_client.get("/api/plans/ike", headers=auth_headers)
        assert resp.json()["content"] == "# The plan"

    async def test_plan_file_outside_plans_dir_is_not_read(
        self, api_client, auth_headers, state_svc, tmp_path
    ):
        # The recorded path is data from a state file / POST body: a path
        # anywhere else on the machine must not be served back.
        secret = tmp_path / "secret.txt"
        secret.write_text("hunter2")
        state_svc.get_state.return_value = _plan_snapshot(str(secret))
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
        state_svc.get_state.return_value = _plan_snapshot(sneaky)
        resp = await api_client.get("/api/plans/ike", headers=auth_headers)
        assert resp.json()["content"] is None

    async def test_404_when_not_waiting(self, api_client, auth_headers):
        resp = await api_client.get("/api/plans/ike", headers=auth_headers)
        assert resp.status_code == 404


class TestPlanControl:
    async def test_approve_disabled_by_default(self, api_client, auth_headers):
        with patch(f"{_PLANS}.plan_control", new_callable=AsyncMock) as control:
            resp = await api_client.post("/api/plans/ike/approve", headers=auth_headers)
        assert resp.status_code == 403
        control.assert_not_awaited()

    async def test_plan_control_only_targets_registered_agents(
        self, api_client, auth_headers, api_app
    ):
        # "stray" is a live tmux session but not a backbone agent: no keys.
        _enable_plan_control(api_app)
        with patch(f"{_PLANS}.plan_control", new_callable=AsyncMock) as control:
            resp = await api_client.post("/api/plans/stray/approve", headers=auth_headers)
            assert resp.status_code == 404
            assert "not a registered agent" in resp.json()["detail"]
            resp = await api_client.post(
                "/api/plans/stray/respond", json={"input": "1"}, headers=auth_headers
            )
            assert resp.status_code == 404
        control.assert_not_awaited()

    async def test_approve_requires_a_waiting_plan(self, api_client, auth_headers, api_app):
        # An idle Claude agent must never receive Shift+Tab: it would toggle its mode.
        _enable_plan_control(api_app)
        with patch(f"{_PLANS}.plan_control", new_callable=AsyncMock) as control:
            resp = await api_client.post("/api/plans/ike/approve", headers=auth_headers)
        assert resp.status_code == 409
        control.assert_not_awaited()

    async def test_approve_uses_the_agents_runtime(
        self, api_client, auth_headers, api_app, state_svc
    ):
        _enable_plan_control(api_app)
        state_svc.get_state.return_value = _plan_snapshot()
        with patch(
            f"{_PLANS}.plan_control",
            new_callable=AsyncMock,
            return_value=("approved", ["sent Escape [Z to claude"]),
        ) as control:
            resp = await api_client.post("/api/plans/ike/approve", headers=auth_headers)
        assert resp.json()["action"] == "plan_approved"
        control.assert_awaited_once_with("ike", "approve", runtime="claude")

    async def test_runtime_without_plan_mode_is_409_and_nothing_is_typed(
        self, api_client, auth_headers, api_app, state_svc
    ):
        _enable_plan_control(api_app)
        state_svc.get_state.return_value = _plan_snapshot()
        refusal = ("unsupported", ["Codex has no plan mode; nothing was sent"])
        with (
            patch(f"{_PLANS}.plan_control", new_callable=AsyncMock, return_value=refusal),
            patch(f"{_PLANS}.safe_deliver", new_callable=AsyncMock) as deliver,
        ):
            resp = await api_client.post("/api/plans/ike/approve", headers=auth_headers)
        assert resp.status_code == 409
        assert "not available" in resp.json()["detail"]
        deliver.assert_not_awaited()

    async def test_reject_leaves_plan_mode_then_sends_feedback_as_a_message(
        self, api_client, auth_headers, api_app, state_svc
    ):
        _enable_plan_control(api_app)
        state_svc.get_state.return_value = _plan_snapshot()
        with (
            patch(
                f"{_PLANS}.plan_control",
                new_callable=AsyncMock,
                return_value=("rejected", ["sent Escape to claude"]),
            ) as control,
            patch(
                f"{_PLANS}.safe_deliver",
                new_callable=AsyncMock,
                return_value=DeliveryOutcome.DELIVERED,
            ) as deliver,
        ):
            resp = await api_client.post(
                "/api/plans/ike/reject", json={"feedback": "too big"}, headers=auth_headers
            )
        assert resp.json()["action"] == "plan_rejected"
        assert resp.json()["feedback"] == "delivered"
        control.assert_awaited_once_with("ike", "reject", runtime="claude")
        assert deliver.await_args.args[1] == "[via:backbone] Plan rejected: too big"
        assert deliver.await_args.kwargs["delivery_kind"] == "direct_message"

    async def test_reject_requires_plan_waiting(self, api_client, auth_headers, api_app):
        _enable_plan_control(api_app)
        resp = await api_client.post(
            "/api/plans/ike/reject", json={"feedback": "x"}, headers=auth_headers
        )
        assert resp.status_code == 409

    async def test_respond_goes_through_safe_deliver(
        self, api_client, auth_headers, api_app, state_svc
    ):
        _enable_plan_control(api_app)
        state_svc.get_state.return_value = _plan_snapshot()
        with patch(
            f"{_PLANS}.safe_deliver", new_callable=AsyncMock, return_value=DeliveryOutcome.DELIVERED
        ) as deliver:
            resp = await api_client.post(
                "/api/plans/ike/respond", json={"input": "2"}, headers=auth_headers
            )
        assert resp.json()["action"] == "plan_response_sent"
        assert deliver.await_args.args[:2] == ("ike", "2")  # verbatim: no envelope on a plan prompt
        assert deliver.await_args.kwargs["delivery_kind"] == "plan_response"

    async def test_undelivered_response_is_reported_not_queued(
        self, api_client, auth_headers, api_app, state_svc
    ):
        _enable_plan_control(api_app)
        state_svc.get_state.return_value = _plan_snapshot()
        with patch(
            f"{_PLANS}.safe_deliver",
            new_callable=AsyncMock,
            return_value=DeliveryOutcome.AGENT_WORKING,
        ):
            resp = await api_client.post(
                "/api/plans/ike/respond", json={"input": "2"}, headers=auth_headers
            )
        assert resp.status_code == 409
        assert "agent_working" in resp.json()["detail"]
