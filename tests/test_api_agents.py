"""Tests for api/routes/agents.py — agent & session management endpoints."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

from src.agent_state import AgentState, StateSnapshot

# All patches target api.routes.agents.* because the route imports
# functions directly (from src.tmux import list_sessions, etc.)
_AGENTS = "api.routes.agents"


def _idle_snapshot(**overrides) -> StateSnapshot:
    """Build an idle StateSnapshot with optional overrides."""
    defaults = dict(state=AgentState.IDLE, source="push", timestamp=time.time())
    defaults.update(overrides)
    return StateSnapshot(**defaults)


def _processing_snapshot(issue: int = 42) -> StateSnapshot:
    """Build a processing StateSnapshot."""
    return StateSnapshot(
        state=AgentState.PROCESSING_ISSUE,
        current_issue=issue,
        source="push",
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# GET /api/agents
# ---------------------------------------------------------------------------


class TestListAgents:
    async def test_returns_named_entities(self, api_client, auth_headers):
        """Named entities from config.entities.sessions are always included."""
        with (
            patch(f"{_AGENTS}.get_agent_state", new_callable=AsyncMock) as mock_state,
            patch(f"{_AGENTS}.list_sessions", new_callable=AsyncMock) as mock_list,
            patch(f"{_AGENTS}.list_sessions_rich", new_callable=AsyncMock) as mock_rich,
            patch(f"{_AGENTS}.session_exists", new_callable=AsyncMock) as mock_exists,
        ):
            mock_state.return_value = _idle_snapshot()
            mock_list.return_value = []
            mock_rich.return_value = []
            mock_exists.return_value = True

            resp = await api_client.get("/api/agents", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        sessions = [a["session"] for a in data["items"]]
        for expected in ("feynman", "ike", "leo", "ada", "brunel"):
            assert expected in sessions
        assert data["total"] == len(data["items"])

    async def test_includes_discovered_coding_agents(self, api_client, auth_headers):
        """Tmux sessions not in named entities are added as coding agents."""
        with (
            patch(f"{_AGENTS}.get_agent_state", new_callable=AsyncMock) as mock_state,
            patch(f"{_AGENTS}.list_sessions", new_callable=AsyncMock) as mock_list,
            patch(f"{_AGENTS}.list_sessions_rich", new_callable=AsyncMock) as mock_rich,
            patch(f"{_AGENTS}.session_exists", new_callable=AsyncMock) as mock_exists,
        ):
            mock_state.return_value = _idle_snapshot()
            mock_list.return_value = [
                "feynman",
                "ike",
                "leo",
                "ada",
                "brunel",
                "platform-api",
            ]
            mock_rich.return_value = [
                {"name": "feynman", "windows": 2, "created": 1708000000, "attached": True},
                {"name": "platform-api", "windows": 1, "created": 1708001000, "attached": False},
            ]
            mock_exists.return_value = True

            resp = await api_client.get("/api/agents", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        sessions = [a["session"] for a in data["items"]]
        assert "platform-api" in sessions
        coding = next(a for a in data["items"] if a["session"] == "platform-api")
        assert coding["role"] == "Coding Agent"
        # Verify tmux enrichment
        assert coding["tmux_windows"] == 1
        assert coding["tmux_attached"] is False
        assert coding["tmux_created"] is not None

    async def test_requires_auth(self, api_client):
        """Request without auth headers is rejected when API key is set."""
        import os

        os.environ["BACKBONE_API_KEY"] = "secret"
        try:
            resp = await api_client.get("/api/agents")
            assert resp.status_code == 401
        finally:
            os.environ.pop("BACKBONE_API_KEY", None)


# ---------------------------------------------------------------------------
# GET /api/agents/{session}/state
# ---------------------------------------------------------------------------


class TestGetAgentState:
    async def test_returns_state_detail(self, api_client, auth_headers):
        """Returns detailed state snapshot for a session."""
        snapshot = _processing_snapshot(issue=99)
        with patch(f"{_AGENTS}.get_agent_state", new_callable=AsyncMock) as mock_state:
            mock_state.return_value = snapshot

            resp = await api_client.get("/api/agents/feynman/state", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] == "feynman"
        assert data["state"] == "processing_issue"
        assert data["current_issue"] == 99
        assert data["source"] == "push"

    async def test_unknown_session_returns_default_state(self, api_client, auth_headers):
        """An unknown session still returns a snapshot (with default/unknown state)."""
        with patch(f"{_AGENTS}.get_agent_state", new_callable=AsyncMock) as mock_state:
            mock_state.return_value = _idle_snapshot(state=AgentState.UNKNOWN, source="default")

            resp = await api_client.get("/api/agents/nonexistent/state", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] == "nonexistent"
        assert data["state"] == "unknown"


# ---------------------------------------------------------------------------
# POST /api/agents/{session}/message
# ---------------------------------------------------------------------------


class TestSendMessage:
    async def test_sends_message_successfully(self, api_client, auth_headers):
        """Sends message to a valid session and returns ok."""
        with patch(
            f"{_AGENTS}.send_message", new_callable=AsyncMock, return_value=True
        ) as mock_send:
            resp = await api_client.post(
                "/api/agents/feynman/message",
                json={"message": "Hello Feynman"},
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session"] == "feynman"
        mock_send.assert_awaited_once_with("feynman", "Hello Feynman")

    async def test_empty_message_returns_400(self, api_client, auth_headers):
        """Empty message body is rejected with 400."""
        resp = await api_client.post(
            "/api/agents/feynman/message",
            json={"message": ""},
            headers=auth_headers,
        )

        assert resp.status_code == 400
        assert "required" in resp.json()["detail"].lower()

    async def test_session_not_found_returns_404(self, api_client, auth_headers):
        """When send_message fails (session missing), returns 404."""
        with patch(f"{_AGENTS}.send_message", new_callable=AsyncMock, return_value=False):
            resp = await api_client.post(
                "/api/agents/ghost/message",
                json={"message": "Are you there?"},
                headers=auth_headers,
            )

        assert resp.status_code == 404
        assert "ghost" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    async def test_returns_session_list(self, api_client, auth_headers):
        """Returns the list of active tmux sessions."""
        with patch(
            f"{_AGENTS}.list_sessions",
            new_callable=AsyncMock,
            return_value=["feynman", "ike", "platform-api"],
        ):
            resp = await api_client.get("/api/sessions", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json() == ["feynman", "ike", "platform-api"]


# ---------------------------------------------------------------------------
# GET /api/sessions/{name}/terminal
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /api/agents/{session}/start
# ---------------------------------------------------------------------------


class TestStartSession:
    async def test_start_session_success(self, api_client, auth_headers):
        """Starting a new session returns ok=True."""
        with patch(f"{_AGENTS}.start_session", new_callable=AsyncMock, return_value=True):
            resp = await api_client.post("/api/agents/test-agent/start", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session"] == "test-agent"

    async def test_start_already_exists(self, api_client, auth_headers):
        """Starting an existing session still returns ok=True (idempotent)."""
        with patch(f"{_AGENTS}.start_session", new_callable=AsyncMock, return_value=True):
            resp = await api_client.post("/api/agents/feynman/start", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# POST /api/agents/{session}/stop
# ---------------------------------------------------------------------------


class TestStopSession:
    async def test_stop_session_success(self, api_client, auth_headers):
        """Stopping a running session returns ok=True."""
        with patch(f"{_AGENTS}.stop_session", new_callable=AsyncMock, return_value=True):
            resp = await api_client.post("/api/agents/feynman/stop", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session"] == "feynman"

    async def test_stop_session_failure(self, api_client, auth_headers):
        """When stop fails, returns ok=False."""
        with patch(f"{_AGENTS}.stop_session", new_callable=AsyncMock, return_value=False):
            resp = await api_client.post("/api/agents/ghost/stop", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# GET /api/sessions/{name}/terminal
# ---------------------------------------------------------------------------


class TestGetTerminalOutput:
    async def test_captures_pane_output(self, api_client, auth_headers):
        """Returns captured terminal output from a session."""
        with patch(
            f"{_AGENTS}.capture_pane",
            new_callable=AsyncMock,
            return_value="$ echo hello\nhello\n$",
        ):
            resp = await api_client.get("/api/sessions/feynman/terminal", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] == "feynman"
        assert "hello" in data["content"]
        assert data["lines"] == 50  # default

    async def test_nonexistent_session_returns_404(self, api_client, auth_headers):
        """When capture returns empty and session does not exist, returns 404."""
        with (
            patch(
                f"{_AGENTS}.capture_pane",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                f"{_AGENTS}.session_exists",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            resp = await api_client.get("/api/sessions/ghost/terminal", headers=auth_headers)

        assert resp.status_code == 404
        assert "ghost" in resp.json()["detail"]
