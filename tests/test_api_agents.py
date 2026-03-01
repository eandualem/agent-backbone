"""Tests for api/routes/agents.py — agent & session management endpoints."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import api.routes.agents as agents_module
from agent_backbone.services.state import AgentState, StateSnapshot
from api.deps import get_state_service, get_tmux_service
from api.routes.agents import _resolve_command

# Still needed for _RUNTIMES/_FALLBACK_DIRS patching (module-level dicts, not services)
_AGENTS = "api.routes.agents"


@pytest.fixture(autouse=True)
def _reset_agents_cache():
    """Reset the TTL cache before each test to prevent cross-test leakage."""
    agents_module._agents_cache = []
    agents_module._agents_cache_ts = 0
    yield
    agents_module._agents_cache = []
    agents_module._agents_cache_ts = 0


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


def _tmux(
    name: str,
    *,
    windows: int = 1,
    created: int = 1708000000,
    attached: bool = False,
) -> dict:
    """Build a rich tmux session dict for test data."""
    return {
        "name": name,
        "windows": windows,
        "created": created,
        "attached": attached,
    }


def _make_mock_state_svc(**kwargs) -> MagicMock:
    """Create a mock StateService with get_state returning an idle snapshot."""
    svc = MagicMock()
    svc.get_state = AsyncMock(return_value=kwargs.get("snapshot", _idle_snapshot()))
    return svc


def _make_mock_tmux_svc(
    *,
    rich_sessions: list | None = None,
    sessions: list | None = None,
    capture_output: str = "",
    session_exists_result: bool = True,
) -> MagicMock:
    """Create a mock TmuxService with configurable return values."""
    svc = MagicMock()
    svc.list_sessions_rich = AsyncMock(return_value=rich_sessions or [])
    svc.list_sessions = AsyncMock(return_value=sessions or [])
    svc.capture_pane = AsyncMock(return_value=capture_output)
    svc.session_exists = AsyncMock(return_value=session_exists_result)
    return svc


def _set_di_overrides(api_app, *, state_svc=None, tmux_svc=None):
    """Set dependency overrides on the FastAPI app."""
    if state_svc is not None:
        api_app.dependency_overrides[get_state_service] = lambda: state_svc
    if tmux_svc is not None:
        api_app.dependency_overrides[get_tmux_service] = lambda: tmux_svc


def _clear_di_overrides(api_app):
    """Remove DI overrides for state and tmux services."""
    api_app.dependency_overrides.pop(get_state_service, None)
    api_app.dependency_overrides.pop(get_tmux_service, None)


# ---------------------------------------------------------------------------
# _resolve_command
# ---------------------------------------------------------------------------


class TestResolveCommand:
    def test_none_command_returns_none(self):
        """None input (shell runtime) returns None."""
        assert _resolve_command(None) is None

    def test_resolves_system_binary(self):
        """Finds a binary on system PATH (e.g. 'python3')."""
        result = _resolve_command("python3")
        assert result is not None
        assert "python3" in result

    def test_unresolvable_returns_none(self):
        """Unknown binary returns None."""
        result = _resolve_command("nonexistent-binary-xyz-12345")
        assert result is None

    def test_fallback_dir_resolution(self, tmp_path):
        """Finds binary in fallback directory when not on PATH."""
        fake_bin = tmp_path / "my-tool"
        fake_bin.touch()
        with patch(f"{_AGENTS}._FALLBACK_DIRS", [tmp_path]):
            result = _resolve_command("my-tool")
        assert result == str(fake_bin)


# ---------------------------------------------------------------------------
# GET /api/agents
# ---------------------------------------------------------------------------


class TestListAgents:
    async def test_returns_named_entities(self, api_app, api_client, auth_headers):
        """Named entities from config.registry.sessions_map are always included."""
        _set_di_overrides(
            api_app,
            state_svc=_make_mock_state_svc(),
            tmux_svc=_make_mock_tmux_svc(),
        )
        try:
            resp = await api_client.get("/api/agents", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        sessions = [a["session"] for a in data["items"]]
        # All 9 named entities from _DEFAULT_SESSIONS must be present
        for expected in (
            "feynman",
            "ike",
            "leo",
            "ada",
            "brunel",
            "hamilton",
            "curie",
            "bell",
            "gallup",
        ):
            assert expected in sessions
        assert data["total"] == len(data["items"])
        # Named entities have type "named_entity"
        for agent in data["items"]:
            assert agent["type"] == "named_entity"

    async def test_includes_discovered_coding_agents(self, api_app, api_client, auth_headers):
        """Tmux sessions not in named entities are added as coding agents."""
        _set_di_overrides(
            api_app,
            state_svc=_make_mock_state_svc(),
            tmux_svc=_make_mock_tmux_svc(
                rich_sessions=[
                    _tmux("feynman", windows=2, attached=True),
                    _tmux("ike"),
                    _tmux("leo"),
                    _tmux("ada"),
                    _tmux("brunel"),
                    _tmux("platform-api", created=1708001000),
                ],
            ),
        )
        try:
            resp = await api_client.get("/api/agents", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        sessions = [a["session"] for a in data["items"]]
        assert "platform-api" in sessions
        coding = next(a for a in data["items"] if a["session"] == "platform-api")
        assert coding["role"] == "Coding Agent"
        assert coding["type"] == "coding_agent"
        # Verify tmux enrichment
        assert coding["tmux_windows"] == 1
        assert coding["tmux_attached"] is False
        assert coding["tmux_created"] is not None

    async def test_excludes_service_sessions(self, api_app, api_client, auth_headers):
        """Service sessions (ngrok, prefect, etc.) are filtered from agents list."""
        _set_di_overrides(
            api_app,
            state_svc=_make_mock_state_svc(),
            tmux_svc=_make_mock_tmux_svc(
                rich_sessions=[
                    _tmux("feynman"),
                    _tmux("ike"),
                    _tmux("leo"),
                    _tmux("ada"),
                    _tmux("brunel"),
                    _tmux("platform-api"),
                    _tmux("ngrok"),
                    _tmux("prefect-worker"),
                    _tmux("prefect-server"),
                    _tmux("telegram-bot"),
                ],
            ),
        )
        try:
            resp = await api_client.get("/api/agents", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        sessions = [a["session"] for a in data["items"]]
        # Coding agent is included
        assert "platform-api" in sessions
        # Services are excluded
        for svc in ("ngrok", "prefect-worker", "prefect-server", "telegram-bot"):
            assert svc not in sessions

    async def test_includes_offline_repos_from_registry(self, api_app, api_client, auth_headers):
        """All repos from registry appear as coding agents, even without active sessions."""
        from dataclasses import replace as dc_replace

        from agent_backbone.services.registry import RepoInfo

        # Add repos to the test config's registry
        old_config = api_app.state.config
        old_reg = old_config.registry
        new_reg = dc_replace(
            old_reg,
            repos=[
                RepoInfo(org="Arclio", name="platform-api", path="/ws/code/Arclio/platform-api"),
                RepoInfo(org="WF", name="agent-backbone", path="/ws/code/WF/agent-backbone"),
            ],
        )
        api_app.state.config = dc_replace(old_config, registry=new_reg)

        _set_di_overrides(
            api_app,
            state_svc=_make_mock_state_svc(),
            tmux_svc=_make_mock_tmux_svc(
                rich_sessions=[
                    {
                        "name": "agent-backbone",
                        "windows": 1,
                        "created": 1708000000,
                        "attached": True,
                    }
                ],
            ),
        )
        try:
            resp = await api_client.get("/api/agents", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)
            api_app.state.config = old_config

        assert resp.status_code == 200
        data = resp.json()
        sessions = [a["session"] for a in data["items"]]

        # platform-api appears even though it has no active tmux session
        assert "platform-api" in sessions
        pa = next(a for a in data["items"] if a["session"] == "platform-api")
        assert pa["type"] == "coding_agent"
        assert pa["online"] is False
        assert pa["org"] == "Arclio"

        # agent-backbone appears via active session discovery (not duplicated)
        ab = next(a for a in data["items"] if a["session"] == "agent-backbone")
        assert ab["type"] == "coding_agent"
        assert ab["online"] is True
        assert ab["tmux_windows"] == 1

        # No duplicates
        assert sessions.count("agent-backbone") == 1
        assert sessions.count("platform-api") == 1

    async def test_excludes_service_entity_type(self, api_app, api_client, auth_headers):
        """Entities with entity_type='service' are excluded from agent list."""
        from dataclasses import replace as dc_replace

        from agent_backbone.services.registry import EntityEntry

        old_config = api_app.state.config
        old_reg = old_config.registry
        new_entities = dict(old_reg.entities)
        new_entities["jarvis"] = EntityEntry(
            session="jarvis",
            home="~/ws/jarvis/",
            groups=[],
            figure="Jarvis",
            role="Personal Assistant",
            entity_type="service",
        )
        new_reg = dc_replace(old_reg, entities=new_entities)
        api_app.state.config = dc_replace(old_config, registry=new_reg)

        _set_di_overrides(
            api_app,
            state_svc=_make_mock_state_svc(),
            tmux_svc=_make_mock_tmux_svc(),
        )
        try:
            resp = await api_client.get("/api/agents", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)
            api_app.state.config = old_config

        assert resp.status_code == 200
        data = resp.json()
        sessions = [a["session"] for a in data["items"]]
        entities = [a["entity"] for a in data["items"]]
        assert "jarvis" not in sessions
        assert "jarvis" not in entities
        # Other named entities still present
        assert "ike" in entities

    async def test_entity_type_field_returned(self, api_app, api_client, auth_headers):
        """EnrichedAgent includes entity_type field defaulting to 'agent'."""
        _set_di_overrides(
            api_app,
            state_svc=_make_mock_state_svc(),
            tmux_svc=_make_mock_tmux_svc(),
        )
        try:
            resp = await api_client.get("/api/agents", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        # All agents should have entity_type "agent" (default)
        for agent in data["items"]:
            assert agent["entity_type"] == "agent"

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
    async def test_returns_state_detail(self, api_app, api_client, auth_headers):
        """Returns detailed state snapshot for a session."""
        snapshot = _processing_snapshot(issue=99)
        _set_di_overrides(
            api_app,
            state_svc=_make_mock_state_svc(snapshot=snapshot),
        )
        try:
            resp = await api_client.get("/api/agents/feynman/state", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] == "feynman"
        assert data["state"] == "processing_issue"
        assert data["current_issue"] == 99
        assert data["source"] == "push"

    async def test_unknown_session_returns_default_state(self, api_app, api_client, auth_headers):
        """An unknown session still returns a snapshot (with default/unknown state)."""
        _set_di_overrides(
            api_app,
            state_svc=_make_mock_state_svc(
                snapshot=_idle_snapshot(state=AgentState.UNKNOWN, source="default")
            ),
        )
        try:
            resp = await api_client.get("/api/agents/nonexistent/state", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] == "nonexistent"
        assert data["state"] == "unknown"


# ---------------------------------------------------------------------------
# GET /api/sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    async def test_returns_session_list(self, api_app, api_client, auth_headers):
        """Returns the list of active tmux sessions."""
        _set_di_overrides(
            api_app,
            tmux_svc=_make_mock_tmux_svc(sessions=["feynman", "ike", "platform-api"]),
        )
        try:
            resp = await api_client.get("/api/sessions", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        assert resp.json() == ["feynman", "ike", "platform-api"]


# ---------------------------------------------------------------------------
# GET /api/sessions/{name}/terminal
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /api/runtimes
# ---------------------------------------------------------------------------


class TestListRuntimes:
    async def test_returns_all_runtimes(self, api_client, auth_headers):
        """Returns all registered runtimes with id and display_name."""
        resp = await api_client.get("/api/runtimes", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        ids = [r["id"] for r in data]
        assert ids == ["claude", "gemini", "codex", "shell"]
        # Verify structure
        claude = next(r for r in data if r["id"] == "claude")
        assert claude["display_name"] == "Claude Code"

    async def test_shows_availability(self, api_client, auth_headers):
        """Runtimes include available field based on resolution."""
        runtimes_with_one_missing = {
            "claude": {
                "display_name": "Claude Code",
                "command": "claude",
                "resolved_path": "/usr/bin/claude",
            },
            "gemini": {
                "display_name": "Gemini CLI",
                "command": "gemini",
                "resolved_path": None,
            },
            "shell": {"display_name": "Plain Shell", "command": None, "resolved_path": None},
        }
        with patch(f"{_AGENTS}._RUNTIMES", runtimes_with_one_missing):
            resp = await api_client.get("/api/runtimes", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        claude = next(r for r in data if r["id"] == "claude")
        gemini = next(r for r in data if r["id"] == "gemini")
        shell = next(r for r in data if r["id"] == "shell")
        assert claude["available"] is True
        assert gemini["available"] is False
        assert shell["available"] is True  # shell has no binary requirement


# ---------------------------------------------------------------------------
# GET /api/sessions/{name}/terminal
# ---------------------------------------------------------------------------


class TestGetTerminalOutput:
    async def test_captures_pane_output(self, api_app, api_client, auth_headers):
        """Returns captured terminal output from a session."""
        _set_di_overrides(
            api_app,
            tmux_svc=_make_mock_tmux_svc(capture_output="$ echo hello\nhello\n$"),
        )
        try:
            resp = await api_client.get("/api/sessions/feynman/terminal", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] == "feynman"
        assert "hello" in data["content"]
        assert data["lines"] == 50  # default

    async def test_nonexistent_session_returns_404(self, api_app, api_client, auth_headers):
        """When capture returns empty and session does not exist, returns 404."""
        _set_di_overrides(
            api_app,
            tmux_svc=_make_mock_tmux_svc(capture_output="", session_exists_result=False),
        )
        try:
            resp = await api_client.get("/api/sessions/ghost/terminal", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 404
        assert "ghost" in resp.json()["detail"]
