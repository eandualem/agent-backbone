"""Tests for the agent endpoints at api/routes/agents.py."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.services.agents import AgentState, StateSnapshot

_ROUTE = "agent_backbone.api.routes.agents"
_FEED = "agent_backbone.api.session_updates"
_LAUNCH = "agent_backbone.services.agents.launch"
_RUNTIME = "agent_backbone.services.runtimes.base.Runtime"

_DETECT_REPO = "agent_backbone.services.agents.store.detect_repo"


def _snapshot(state: AgentState = AgentState.IDLE, **kwargs) -> StateSnapshot:
    return StateSnapshot(state=state, source="push", timestamp=time.time(), **kwargs)


@pytest.fixture
def state_svc():
    """The state read behind the feed and the state endpoint, answering idle."""
    get_state = AsyncMock(return_value=_snapshot())
    with patch(f"{_FEED}.agent_state", get_state), patch(f"{_ROUTE}.agent_state", get_state):
        yield SimpleNamespace(get_state=get_state)


@pytest.fixture
def tmux_svc():
    """The tmux reads and writes behind the feed and the routes."""
    mocks = SimpleNamespace(
        list_sessions=AsyncMock(return_value=["ike", "feynman"]),
        list_sessions_rich=AsyncMock(
            return_value=[
                {"name": "ike", "windows": 1, "created": 1000, "attached": True, "activity": 5},
                {
                    "name": "feynman",
                    "windows": 2,
                    "created": 2000,
                    "attached": False,
                    "activity": 0,
                },
            ]
        ),
        session_exists=AsyncMock(return_value=False),
        stop_session=AsyncMock(return_value=True),
        capture_pane=AsyncMock(return_value="prompt >"),
    )
    with (
        patch(f"{_FEED}.list_sessions_rich", mocks.list_sessions_rich),
        patch(f"{_ROUTE}.list_sessions", mocks.list_sessions),
        patch(f"{_ROUTE}.session_exists", mocks.session_exists),
        # stop and forget go through the shared operations now
        patch("agent_backbone.services.agents.operations.session_exists", mocks.session_exists),
        patch("agent_backbone.services.agents.launch.stop_session", mocks.stop_session),
        patch(f"{_ROUTE}.capture_pane", mocks.capture_pane),
    ):
        yield mocks


@pytest.fixture(autouse=True)
def _override(api_app, state_svc, tmux_svc):
    with (
        patch(
            "agent_backbone.api.session_updates.query_environment_var",
            new_callable=AsyncMock,
            return_value="claude",
        ),
        patch(
            f"{_LAUNCH}.wait_until_ready",
            new_callable=AsyncMock,
            return_value=("ready", ["hook reported idle"]),
        ),
    ):
        yield


@pytest.fixture
def launch():
    """The launch seams under ``start_agent``: no real tmux, no real binaries."""
    with (
        patch(f"{_RUNTIME}.available", return_value=True),
        patch(f"{_LAUNCH}.session_exists", new_callable=AsyncMock, return_value=False) as exists,
        patch(f"{_LAUNCH}.start_session", new_callable=AsyncMock, return_value=True) as start,
        patch(f"{_RUNTIME}.build_command", return_value=["/usr/bin/claude"]) as build,
        patch("agent_backbone.services.runtimes.claude.pre_trust_directory"),
        patch("agent_backbone.services.runtimes.codex.pre_trust_codex_directory") as trust,
    ):
        yield MagicMock(
            session_exists=exists, start_session=start, build_command=build, trust=trust
        )


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
    async def test_start_configured_agent(self, api_client, auth_headers, launch):
        resp = await api_client.post("/api/agents/ike/start", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True and data["runtime"] == "claude"
        assert data["working_directory"].endswith("/ike")
        assert data["ready"] == "ready" and data["evidence"] == ["hook reported idle"]
        launch.start_session.assert_awaited_once()
        kwargs = launch.start_session.await_args.kwargs
        assert kwargs["command"] == ["/usr/bin/claude"]
        assert kwargs["environment"]["BACKBONE_RUNTIME"] == "claude"

    async def test_request_overrides_config(self, api_client, auth_headers, launch):
        launch.build_command.return_value = ["/usr/bin/codex", "--model", "x"]
        resp = await api_client.post(
            "/api/agents/ike/start",
            json={"runtime": "codex", "model": "x", "resume": True},
            headers=auth_headers,
        )
        assert resp.json()["runtime"] == "codex"
        assert launch.start_session.await_args.kwargs["environment"]["BACKBONE_RUNTIME"] == "codex"
        assert launch.build_command.call_args.kwargs["model"] == "x"
        assert launch.build_command.call_args.kwargs["resume"] is True

    async def test_every_runtime_gets_the_trust_setting(self, api_client, auth_headers, launch):
        # `agent start` for codex must pre-trust like the swarm path does, and
        # build_command needs the flag for gemini's --skip-trust.
        resp = await api_client.post(
            "/api/agents/ike/start", json={"runtime": "codex"}, headers=auth_headers
        )
        assert resp.status_code == 200
        launch.trust.assert_called_once()
        assert launch.start_session.await_args.kwargs["environment"]["BACKBONE_RUNTIME"] == "codex"
        assert launch.build_command.call_args.kwargs["pre_trust"] is True

    async def test_runtime_without_launch_injection_gets_brief_queued(
        self, api_client, auth_headers, launch, api_app
    ):
        launch.build_command.return_value = ["/usr/bin/aider"]
        resp = await api_client.post(
            "/api/agents/ike/start", json={"runtime": "aider"}, headers=auth_headers
        )
        assert resp.status_code == 200
        queued = await api_app.state.db.queue.dequeue("ike")
        assert len(queued) == 1
        assert queued[0]["message"].startswith("[via:backbone] ")
        assert queued[0]["delivery_kind"] == "direct_message"
        assert queued[0]["source"] == "agent-brief"

    async def test_launch_injected_and_resumed_runtimes_are_not_rebriefed(
        self, api_client, auth_headers, launch, api_app
    ):
        await api_client.post("/api/agents/ike/start", headers=auth_headers)  # claude
        await api_client.post(
            "/api/agents/ike/start", json={"runtime": "shell"}, headers=auth_headers
        )
        await api_client.post(
            "/api/agents/ike/start",
            json={"runtime": "aider", "resume": True},
            headers=auth_headers,
        )
        assert await api_app.state.db.queue.sessions_with_pending() == []

    async def test_unknown_runtime_400(self, api_client, auth_headers):
        resp = await api_client.post(
            "/api/agents/ike/start", json={"runtime": "nope"}, headers=auth_headers
        )
        assert resp.status_code == 400

    async def test_unavailable_binary_400(self, api_client, auth_headers):
        with patch("agent_backbone.services.runtimes.base.Runtime.available", return_value=False):
            resp = await api_client.post("/api/agents/ike/start", headers=auth_headers)
        assert resp.status_code == 400
        assert "binary not found" in resp.json()["detail"]

    async def test_idempotent_when_session_exists(self, api_client, auth_headers, launch):
        launch.session_exists.return_value = True
        resp = await api_client.post("/api/agents/ike/start", headers=auth_headers)
        assert resp.json()["already_existed"] is True
        launch.start_session.assert_not_awaited()

    async def test_unknown_agent_requires_dir(self, api_client, auth_headers, launch):
        resp = await api_client.post("/api/agents/scratch/start", headers=auth_headers)
        assert resp.status_code == 404
        assert "not a known agent" in resp.json()["detail"]

    async def test_start_with_dir_discovers_and_registers(
        self, api_client, auth_headers, launch, api_app, tmp_path
    ):
        project = tmp_path / "scratch-app"
        project.mkdir()
        launch.build_command.return_value = None
        with patch(
            "agent_backbone.services.agents.store.detect_repo",
            new_callable=AsyncMock,
            return_value="acme/scratch",
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
        assert launch.start_session.await_args.kwargs["working_dir"] == str(project.resolve())
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
            tmux_svc.capture_pane.return_value = "❯ "
            resp = await api_client.get("/api/agents/ike/inspect", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["state"] == "busy" and data["delivery"] == "agent_working"
        assert data["current_issue"] == 4 and data["current_repo"] == "example/ike"
        assert data["evidence"] == ["hook state 'busy' written 3s ago (fresh)"]
        assert data["known"] is True and data["online"] is True

    async def test_inspect_carries_the_session_id_and_last_reply(
        self, api_client, auth_headers, tmux_svc
    ):
        tmux_svc.session_exists.return_value = True
        from agent_backbone.services.routing.models import SessionIntelligence, SessionProfile

        with patch(f"{_ROUTE}.get_session_intelligence", new_callable=AsyncMock) as intel:
            intel.return_value = SessionProfile(
                "ike",
                SessionIntelligence.READY,
                runtime="claude",
                agent_state=AgentState.IDLE,
                session_id="01a0-sess",
                last_message="Shipped it.",
                detail="resets at 3 PM",
            )
            resp = await api_client.get("/api/agents/ike/inspect", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["session_id"] == "01a0-sess"
        assert resp.json()["last_message"] == "Shipped it."
        assert resp.json()["detail"] == "resets at 3 PM"

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

    async def test_unregistered_tmux_session_is_out_of_reach(
        self, api_client, auth_headers, tmux_svc
    ):
        # The API key is a backbone credential, not a shell: stopping an
        # arbitrary tmux session through a registered-looking path is a 404
        # and never reaches tmux.
        resp = await api_client.post("/api/agents/stray/stop", headers=auth_headers)
        assert resp.status_code == 404
        assert "not a registered agent" in resp.json()["detail"]
        tmux_svc.stop_session.assert_not_awaited()

    async def test_refuses_to_stop_backbone_session(self, api_client, auth_headers, api_app):
        from dataclasses import replace

        from agent_backbone.config import BackboneSection

        api_app.state.config = replace(
            api_app.state.config, backbone=BackboneSection(session_name="ike")
        )
        resp = await api_client.post("/api/agents/ike/stop", headers=auth_headers)
        assert resp.status_code == 400


class TestSessions:
    async def test_list_sessions(self, api_client, auth_headers):
        resp = await api_client.get("/api/sessions", headers=auth_headers)
        assert resp.json() == ["ike", "feynman"]

    async def test_terminal_output(self, api_client, auth_headers):
        resp = await api_client.get("/api/sessions/ike/terminal?lines=10", headers=auth_headers)
        assert resp.json()["content"] == "prompt >"

    async def test_terminal_output_unregistered_session_404(
        self, api_client, auth_headers, tmux_svc
    ):
        # A tmux session that is not a backbone agent is out of the API's reach,
        # even though the same user could capture it.
        tmux_svc.session_exists.return_value = True
        resp = await api_client.get("/api/sessions/stray/terminal", headers=auth_headers)
        assert resp.status_code == 404
        assert "not a registered agent" in resp.json()["detail"]
        tmux_svc.capture_pane.assert_not_awaited()

    async def test_terminal_output_missing_session(self, api_client, auth_headers, tmux_svc):
        tmux_svc.capture_pane.return_value = ""
        tmux_svc.session_exists.return_value = False
        resp = await api_client.get("/api/sessions/nope/terminal", headers=auth_headers)
        assert resp.status_code == 404


class TestApproveAgent:
    async def test_approves_and_records_an_event(self, api_client, auth_headers, api_app):
        with patch(
            f"{_ROUTE}.approve_agent",
            new_callable=AsyncMock,
            return_value=("approved", ["answered with Enter; prompt cleared", "Bash command"]),
        ) as approve:
            resp = await api_client.post(
                "/api/agents/ike/approve", json={"from_entity": "orch"}, headers=auth_headers
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] and data["outcome"] == "approved" and data["approved_by"] == "orch"
        assert approve.await_args.kwargs["runtime"] == "claude"
        events = await api_app.state.db.events.query(limit=5)
        assert events and events[0]["event_type"] == "approval"
        assert "orch approved a claude permission prompt on ike" in events[0]["summary"]

    async def test_not_waiting_is_409_and_nothing_is_typed(self, api_client, auth_headers):
        with patch(
            f"{_ROUTE}.approve_agent",
            new_callable=AsyncMock,
            return_value=("not_waiting", ["terminal shows no permission prompt:", "❯"]),
        ):
            resp = await api_client.post("/api/agents/ike/approve", headers=auth_headers)
        assert resp.status_code == 409
        assert resp.json()["detail"]["outcome"] == "not_waiting"

    async def test_unregistered_agent_404(self, api_client, auth_headers):
        with patch(f"{_ROUTE}.approve_agent", new_callable=AsyncMock) as approve:
            resp = await api_client.post("/api/agents/stray/approve", headers=auth_headers)
        assert resp.status_code == 404
        approve.assert_not_awaited()

    async def test_disabled_by_setting(self, api_client, auth_headers, api_app):
        from dataclasses import replace

        from agent_backbone.config import SecurityConfig

        api_app.state.config = replace(
            api_app.state.config, security=SecurityConfig(allow_remote_approval=False)
        )
        with patch(f"{_ROUTE}.approve_agent", new_callable=AsyncMock) as approve:
            resp = await api_client.post("/api/agents/ike/approve", headers=auth_headers)
        assert resp.status_code == 403
        approve.assert_not_awaited()


class TestPostAgentState:
    async def test_writes_the_hook_state_file(self, api_client, auth_headers, api_app):
        from agent_backbone.services.agents import get_agent_state, read_state_file

        resp = await api_client.post(
            "/api/agents/ike/state",
            json={"state": "busy", "issue": 7, "ts": time.time()},
            headers=auth_headers,
        )
        assert resp.json() == {"ok": True, "session": "ike"}
        state_dir = api_app.state.config.state_dir
        push = read_state_file(state_dir, "ike")
        assert push.state == AgentState.BUSY and push.current_issue == 7
        # ...which is exactly what delivery decisions read.
        snapshot = await get_agent_state(state_dir, "ike", stale_threshold=300)
        assert snapshot.state == AgentState.BUSY and snapshot.source == "push"

    async def test_plan_fields_stored(self, api_client, auth_headers, api_app):
        from agent_backbone.services.agents import read_state_file

        await api_client.post(
            "/api/agents/ike/state",
            json={
                "state": "waiting_for_human",
                "reason": "plan",
                "plan_file": "/p.md",
                "plan_title": "T",
            },
            headers=auth_headers,
        )
        push = read_state_file(api_app.state.config.state_dir, "ike")
        assert push.is_plan_waiting and push.plan_file == "/p.md" and push.plan_title == "T"

    async def test_only_registered_agents(self, api_client, auth_headers):
        resp = await api_client.post(
            "/api/agents/stray/state", json={"state": "busy"}, headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_unknown_state_is_rejected_not_silently_unknown(
        self, api_client, auth_headers, api_app
    ):
        from agent_backbone.services.agents import read_state_file

        resp = await api_client.post(
            "/api/agents/leo/state", json={"state": "napping"}, headers=auth_headers
        )
        assert resp.status_code == 422
        assert read_state_file(api_app.state.config.state_dir, "leo") is None

    async def test_negative_issue_is_rejected(self, api_client, auth_headers):
        resp = await api_client.post(
            "/api/agents/ike/state",
            json={"state": "busy", "issue": -5},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_far_future_ts_is_rejected(self, api_client, auth_headers):
        # A ts that stays "fresh" forever would make the push permanently
        # authoritative over the terminal.
        resp = await api_client.post(
            "/api/agents/ike/state",
            json={"state": "idle", "ts": 9999999999},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_legacy_zero_ts_still_accepted(self, api_client, auth_headers, api_app):
        from agent_backbone.services.agents import read_state_file

        resp = await api_client.post(
            "/api/agents/ike/state", json={"state": "busy", "ts": 0}, headers=auth_headers
        )
        assert resp.status_code == 200
        assert read_state_file(api_app.state.config.state_dir, "ike") is not None


class TestDeny:
    async def test_denies_and_records_an_event(self, api_client, auth_headers, api_app):
        with patch(
            f"{_ROUTE}.deny_agent",
            new_callable=AsyncMock,
            return_value=("denied", ["answered with Escape; prompt cleared", "Switch to gpt-"]),
        ) as deny:
            resp = await api_client.post(
                "/api/agents/ike/deny", json={"from_entity": "orch"}, headers=auth_headers
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] and data["outcome"] == "denied" and data["denied_by"] == "orch"
        deny.assert_awaited_once()
        events = await api_app.state.db.events.query(limit=5)
        assert events and events[0]["event_type"] == "denial"
        assert "orch denied a claude permission prompt on ike" in events[0]["summary"]

    async def test_a_choice_dialog_cannot_be_approved(self, api_client, auth_headers, api_app):
        with patch(
            f"{_ROUTE}.approve_agent",
            new_callable=AsyncMock,
            return_value=("not_permission", ["the dialog on screen is a choice"]),
        ):
            resp = await api_client.post("/api/agents/ike/approve", headers=auth_headers)
        assert resp.status_code == 409
        assert resp.json()["detail"]["outcome"] == "not_permission"
