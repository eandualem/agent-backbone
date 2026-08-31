"""Tests for the agent endpoints at api/routes/agents.py."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.api.deps import get_state_service, get_tmux_service
from agent_backbone.services.agents import AgentState, StateSnapshot

_ROUTE = "agent_backbone.api.routes.agents"


def _snapshot(state: AgentState = AgentState.IDLE, **kwargs) -> StateSnapshot:
    return StateSnapshot(state=state, source="push", timestamp=time.time(), **kwargs)


@pytest.fixture
def state_svc():
    svc = MagicMock()
    svc.get_state = AsyncMock(return_value=_snapshot())
    return svc


@pytest.fixture
def tmux_svc():
    svc = MagicMock()
    svc.list_sessions = AsyncMock(return_value=["ike", "feynman"])
    svc.list_sessions_rich = AsyncMock(
        return_value=[
            {"name": "ike", "windows": 1, "created": 1000, "attached": True, "activity": 5},
            {"name": "feynman", "windows": 2, "created": 2000, "attached": False, "activity": 0},
        ]
    )
    svc.session_exists = AsyncMock(return_value=False)
    svc.start_session = AsyncMock(return_value=True)
    svc.stop_session = AsyncMock(return_value=True)
    svc.capture_pane = AsyncMock(return_value="prompt >")
    return svc


@pytest.fixture(autouse=True)
def _override(api_app, state_svc, tmux_svc):
    api_app.dependency_overrides[get_state_service] = lambda: state_svc
    api_app.dependency_overrides[get_tmux_service] = lambda: tmux_svc
    with (
        patch(
            "agent_backbone.api.session_updates.query_environment_var",
            new_callable=AsyncMock,
            return_value="claude",
        ),
        patch(
            f"{_ROUTE}.wait_until_ready",
            new_callable=AsyncMock,
            return_value=("ready", ["hook reported idle"]),
        ),
    ):
        yield
    api_app.dependency_overrides.clear()


class TestListAgents:
    async def test_returns_configured_agents_with_live_state(self, api_client, auth_headers):
        resp = await api_client.get("/api/agents", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 9
        by_name = {a["name"]: a for a in data["items"]}
        assert by_name["ike"]["online"] is True
        assert by_name["ike"]["state"] == "idle"
        assert by_name["ike"]["configured"] is True
        assert by_name["ike"]["runtime"] == "claude"
        assert by_name["ike"]["tmux_attached"] is True
        assert by_name["leo"]["online"] is False
        assert by_name["leo"]["state"] == "offline"

    async def test_includes_unconfigured_live_sessions(self, api_client, auth_headers, tmux_svc):
        tmux_svc.list_sessions_rich.return_value.append(
            {"name": "scratch", "windows": 1, "created": 1, "attached": False, "activity": 0}
        )
        resp = await api_client.get("/api/agents", headers=auth_headers)
        scratch = next(a for a in resp.json()["items"] if a["name"] == "scratch")
        assert scratch["configured"] is False
        assert scratch["online"] is True

    async def test_requires_auth(self, api_client):
        assert (await api_client.get("/api/agents")).status_code == 401


class TestGetAgentState:
    async def test_returns_state_detail(self, api_client, auth_headers, state_svc):
        state_svc.get_state.return_value = _snapshot(
            AgentState.WAITING_FOR_HUMAN, reason="plan", plan_file="/tmp/plan.md", plan_title="Plan"
        )
        resp = await api_client.get("/api/agents/ike/state", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["state"] == "waiting_for_human"
        assert resp.json()["reason"] == "plan"
        assert resp.json()["plan_title"] == "Plan"


class TestRuntimes:
    async def test_lists_runtimes(self, api_client, auth_headers):
        resp = await api_client.get("/api/runtimes", headers=auth_headers)
        ids = {r["id"] for r in resp.json()}
        assert {"claude", "codex", "gemini", "shell"} <= ids
        shell = next(r for r in resp.json() if r["id"] == "shell")
        assert shell["available"] is True


class TestStartAgent:
    async def test_start_configured_agent(self, api_client, auth_headers, tmux_svc):
        with (
            patch(f"{_ROUTE}.runtime_available", return_value=True),
            patch(f"{_ROUTE}.build_command", return_value=["/usr/bin/claude"]),
        ):
            resp = await api_client.post("/api/agents/ike/start", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True and data["runtime"] == "claude"
        assert data["working_directory"].endswith("/ike")
        assert data["ready"] == "ready" and data["evidence"] == ["hook reported idle"]
        tmux_svc.start_session.assert_awaited_once()
        kwargs = tmux_svc.start_session.await_args.kwargs
        assert kwargs["command"] == ["/usr/bin/claude"]
        assert kwargs["environment"]["BACKBONE_RUNTIME"] == "claude"

    async def test_request_overrides_config(self, api_client, auth_headers):
        with (
            patch(f"{_ROUTE}.runtime_available", return_value=True),
            patch(
                f"{_ROUTE}.build_command", return_value=["/usr/bin/codex", "--model", "x"]
            ) as build,
        ):
            resp = await api_client.post(
                "/api/agents/ike/start",
                json={"runtime": "codex", "model": "x", "resume": True},
                headers=auth_headers,
            )
        assert resp.json()["runtime"] == "codex"
        build.assert_called_once_with("codex", model="x", resume=True)

    async def test_unknown_runtime_400(self, api_client, auth_headers):
        resp = await api_client.post(
            "/api/agents/ike/start", json={"runtime": "nope"}, headers=auth_headers
        )
        assert resp.status_code == 400

    async def test_unavailable_binary_400(self, api_client, auth_headers):
        with patch(f"{_ROUTE}.runtime_available", return_value=False):
            resp = await api_client.post("/api/agents/ike/start", headers=auth_headers)
        assert resp.status_code == 400
        assert "binary not found" in resp.json()["detail"]

    async def test_idempotent_when_session_exists(self, api_client, auth_headers, tmux_svc):
        tmux_svc.session_exists.return_value = True
        with patch(f"{_ROUTE}.runtime_available", return_value=True):
            resp = await api_client.post("/api/agents/ike/start", headers=auth_headers)
        assert resp.json()["already_existed"] is True
        tmux_svc.start_session.assert_not_awaited()

    async def test_unknown_agent_requires_dir(self, api_client, auth_headers, tmux_svc):
        with patch(f"{_ROUTE}.runtime_available", return_value=True):
            resp = await api_client.post("/api/agents/scratch/start", headers=auth_headers)
        assert resp.status_code == 404
        assert "not a known agent" in resp.json()["detail"]

    async def test_start_with_dir_discovers_and_registers(
        self, api_client, auth_headers, tmux_svc, api_app, tmp_path
    ):
        project = tmp_path / "scratch-app"
        project.mkdir()
        with (
            patch(f"{_ROUTE}.runtime_available", return_value=True),
            patch(f"{_ROUTE}.build_command", return_value=None),
            patch("agent_backbone.services.agent_store.detect_repo", return_value="acme/scratch"),
        ):
            resp = await api_client.post(
                "/api/agents/start",
                json={"dir": str(project), "runtime": "shell", "watch": ["acme/other"]},
                headers=auth_headers,
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "scratch-app" and data["repo"] == "acme/scratch"
        assert data["model"] is None
        assert tmux_svc.start_session.await_args.kwargs["working_dir"] == str(project.resolve())
        spec = api_app.state.agent_store.agents.get("scratch-app")
        assert spec is not None and spec.watches == ("acme/other",)
        # ...and it is now visible via the API
        listed = await api_client.get("/api/config/agents", headers=auth_headers)
        assert "scratch-app" in [a["name"] for a in listed.json()]

    async def test_inspect_reports_evidence(self, api_client, auth_headers, tmux_svc):
        tmux_svc.session_exists.return_value = True
        with patch(
            f"{_ROUTE}.get_session_intelligence",
            new_callable=AsyncMock,
        ) as intel:
            from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile

            intel.return_value = SessionProfile(
                "ike",
                SessionIntelligence.AGENT_WORKING,
                runtime="claude",
                agent_state=AgentState.BUSY,
                current_issue=4,
                current_repo="example/ike",
                evidence=["hook state 'busy' written 3s ago (fresh)"],
            )
            with patch(f"{_ROUTE}.capture_pane", new_callable=AsyncMock, return_value="❯ "):
                resp = await api_client.get("/api/agents/ike/inspect", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["state"] == "busy" and data["delivery"] == "agent_working"
        assert data["current_issue"] == 4 and data["current_repo"] == "example/ike"
        assert data["evidence"] == ["hook state 'busy' written 3s ago (fresh)"]
        assert data["known"] is True and data["online"] is True

    async def test_patch_watch_and_forget(self, api_client, auth_headers, tmux_svc):
        resp = await api_client.patch(
            "/api/agents/ike", json={"description": "Reviews", "model": "m"}, headers=auth_headers
        )
        assert resp.status_code == 200 and resp.json()["model"] == "m"
        resp = await api_client.post(
            "/api/agents/ike/watch", json={"repo": "acme/web"}, headers=auth_headers
        )
        assert "acme/web" in resp.json()["watches"]
        resp = await api_client.post(
            "/api/agents/ike/unwatch", json={"repo": "acme/web"}, headers=auth_headers
        )
        assert "acme/web" not in resp.json()["watches"]
        tmux_svc.session_exists.return_value = True
        assert (await api_client.delete("/api/agents/ike", headers=auth_headers)).status_code == 409
        tmux_svc.session_exists.return_value = False
        assert (await api_client.delete("/api/agents/ike", headers=auth_headers)).json()["ok"]
        assert (await api_client.delete("/api/agents/ike", headers=auth_headers)).status_code == 404


class TestStopAgent:
    async def test_stop_session(self, api_client, auth_headers, tmux_svc):
        resp = await api_client.post("/api/agents/ike/stop", headers=auth_headers)
        assert resp.json() == {"ok": True, "session": "ike"}
        tmux_svc.stop_session.assert_awaited_once_with("ike")

    async def test_refuses_to_stop_backbone_session(self, api_client, auth_headers):
        resp = await api_client.post("/api/agents/backbone/stop", headers=auth_headers)
        assert resp.status_code == 400


class TestSessions:
    async def test_list_sessions(self, api_client, auth_headers):
        resp = await api_client.get("/api/sessions", headers=auth_headers)
        assert resp.json() == ["ike", "feynman"]

    async def test_terminal_output(self, api_client, auth_headers):
        resp = await api_client.get("/api/sessions/ike/terminal?lines=10", headers=auth_headers)
        assert resp.json()["content"] == "prompt >"

    async def test_terminal_output_missing_session(self, api_client, auth_headers, tmux_svc):
        tmux_svc.capture_pane.return_value = ""
        tmux_svc.session_exists.return_value = False
        resp = await api_client.get("/api/sessions/nope/terminal", headers=auth_headers)
        assert resp.status_code == 404


class TestPostAgentState:
    async def test_stores_state(self, api_client, auth_headers, api_app):
        resp = await api_client.post(
            "/api/agents/ike/state",
            json={"state": "busy", "issue": 7, "ts": 123.0},
            headers=auth_headers,
        )
        assert resp.json() == {"ok": True, "session": "ike"}
        row = await api_app.state.db.get_agent_state("ike")
        assert row["state"] == "busy" and row["current_issue"] == 7

    async def test_plan_fields_stored(self, api_client, auth_headers, api_app):
        await api_client.post(
            "/api/agents/ike/state",
            json={"state": "plan_waiting", "plan_file": "/p.md", "plan_title": "T"},
            headers=auth_headers,
        )
        row = await api_app.state.db.get_agent_state("ike")
        assert row["plan_file"] == "/p.md" and row["plan_title"] == "T"
