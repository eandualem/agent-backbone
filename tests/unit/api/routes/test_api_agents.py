"""Tests for api/routes/agents.py — agent & session management endpoints."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent_backbone.api.routes.agents as agents_module
from agent_backbone.api.deps import get_state_service, get_tmux_service
from agent_backbone.api.routes.agents import _resolve_command
from agent_backbone.services.agents import AgentState, StateSnapshot

# Still needed for _RUNTIMES/_FALLBACK_DIRS patching (module-level dicts, not services)
_AGENTS = "agent_backbone.api.routes.agents"


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
    activity: int = 1708000100,
) -> dict:
    """Build a rich tmux session dict for test data."""
    return {
        "name": name,
        "windows": windows,
        "created": created,
        "attached": attached,
        "activity": activity,
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
    start_session_result: bool = True,
    stop_session_result: bool = True,
) -> MagicMock:
    """Create a mock TmuxService with configurable return values."""
    svc = MagicMock()
    svc.list_sessions_rich = AsyncMock(return_value=rich_sessions or [])
    svc.list_sessions = AsyncMock(return_value=sessions or [])
    svc.capture_pane = AsyncMock(return_value=capture_output)
    svc.session_exists = AsyncMock(return_value=session_exists_result)
    svc.start_session = AsyncMock(return_value=start_session_result)
    svc.stop_session = AsyncMock(return_value=stop_session_result)
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

    async def test_role_instances_do_not_emit_role_alias_or_repo_phantom(
        self, api_app, api_client, auth_headers, tmp_path
    ):
        """Flat role-instance registries surface concrete sessions only."""
        from dataclasses import replace as dc_replace

        from agent_backbone.services.registry import build_registry

        registry_file = tmp_path / "entities.json"
        code_dir = tmp_path / "code"
        (code_dir / "WF" / "bell").mkdir(parents=True)
        (code_dir / "Loveble" / "bell").mkdir(parents=True)
        (code_dir / "WF" / "agent-backbone").mkdir(parents=True)
        registry_file.write_text(
            json.dumps(
                {
                    "bell-wf": {
                        "session": "bell-wf",
                        "home": str(code_dir / "WF" / "bell"),
                        "groups": ["orchestrators"],
                        "figure": "Alexander Graham Bell",
                        "role": "Org Orchestrator",
                        "organization": "WF",
                        "type": "role-instance",
                        "roleDefinition": "~/orchestration/roles/bell/",
                        "roleEntity": "bell",
                    },
                    "bell-loveble": {
                        "session": "bell-loveble",
                        "home": str(code_dir / "Loveble" / "bell"),
                        "groups": ["orchestrators"],
                        "figure": "Alexander Graham Bell",
                        "role": "Org Orchestrator",
                        "organization": "Loveble",
                        "type": "role-instance",
                        "roleDefinition": "~/orchestration/roles/bell/",
                        "roleEntity": "bell",
                    },
                }
            )
        )

        old_config = api_app.state.config
        new_registry = build_registry(registry_file, code_dir)
        api_app.state.config = dc_replace(old_config, registry=new_registry)

        _set_di_overrides(
            api_app,
            state_svc=_make_mock_state_svc(),
            tmux_svc=_make_mock_tmux_svc(
                rich_sessions=[
                    _tmux("bell-wf"),
                    _tmux("bell-loveble"),
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
        sessions = [agent["session"] for agent in data["items"]]

        assert "bell" not in sessions
        assert "agent-backbone" in sessions

        bell_wf = next(agent for agent in data["items"] if agent["session"] == "bell-wf")
        bell_loveble = next(agent for agent in data["items"] if agent["session"] == "bell-loveble")
        assert bell_wf["type"] == "named_entity"
        assert bell_wf["entity_type"] == "role-instance"
        assert bell_loveble["type"] == "named_entity"
        assert bell_loveble["entity_type"] == "role-instance"

        coding = next(agent for agent in data["items"] if agent["session"] == "agent-backbone")
        assert coding["type"] == "coding_agent"

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

    async def test_session_name_case_preserved(self, api_app, api_client, auth_headers):
        """Mixed-case session names are preserved (not lowercased)."""
        snapshot = _processing_snapshot(issue=99)
        _set_di_overrides(
            api_app,
            state_svc=_make_mock_state_svc(snapshot=snapshot),
        )
        try:
            resp = await api_client.get("/api/agents/Feynman/state", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] == "Feynman"

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
        assert ids == ["claude", "gemini", "codex", "cursor", "opencode", "shell"]
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


# ---------------------------------------------------------------------------
# POST /api/agents/{session}/start
# ---------------------------------------------------------------------------


class TestStartAgent:
    async def test_start_default_runtime(self, api_app, api_client, auth_headers):
        """Default runtime (claude) starts a session with resolved binary."""
        tmux_svc = _make_mock_tmux_svc(session_exists_result=False)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        try:
            with patch(
                "agent_backbone.api.routes.agents.resolve_agent_dir",
                return_value="/ws/code/WF/my-repo",
            ):
                resp = await api_client.post("/api/agents/my-repo/start", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session"] == "my-repo"
        assert data["runtime"] == "claude"
        assert data["working_directory"] == "/ws/code/WF/my-repo"
        assert data["already_existed"] is False
        tmux_svc.start_session.assert_awaited_once()
        call_kwargs = tmux_svc.start_session.call_args.kwargs
        assert call_kwargs["environment"] == {"BACKBONE_RUNTIME": "claude"}

    async def test_start_with_model_and_resume(self, api_app, api_client, auth_headers):
        """Model and resume flags are passed into the command list."""
        tmux_svc = _make_mock_tmux_svc(session_exists_result=False)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        try:
            with patch(
                "agent_backbone.api.routes.agents.resolve_agent_dir",
                return_value="/ws/code/WF/my-repo",
            ):
                resp = await api_client.post(
                    "/api/agents/my-repo/start",
                    json={"model": "claude-opus-4-6", "resume": True},
                    headers=auth_headers,
                )
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "claude-opus-4-6"
        # Verify the command list passed to start_session
        call_kwargs = tmux_svc.start_session.call_args[1]
        cmd = call_kwargs["command"]
        assert "--model" in cmd
        assert "claude-opus-4-6" in cmd
        assert "--resume" in cmd
        assert call_kwargs["environment"] == {"BACKBONE_RUNTIME": "claude"}

    async def test_start_preserves_session_case(self, api_app, api_client, auth_headers):
        """Mixed-case session names are preserved through start."""
        tmux_svc = _make_mock_tmux_svc(session_exists_result=False)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        try:
            with patch(
                "agent_backbone.api.routes.agents.resolve_agent_dir",
                return_value="/ws/code/AI-chatbot/",
            ):
                resp = await api_client.post("/api/agents/AI-chatbot/start", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] == "AI-chatbot"
        # Verify tmux was called with original casing
        tmux_svc.session_exists.assert_awaited_once_with("AI-chatbot")

    async def test_start_unknown_runtime_400(self, api_app, api_client, auth_headers):
        """Unknown runtime returns 400."""
        tmux_svc = _make_mock_tmux_svc(session_exists_result=False)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        try:
            resp = await api_client.post(
                "/api/agents/my-repo/start",
                json={"runtime": "nonexistent-rt"},
                headers=auth_headers,
            )
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 400
        assert "Unknown runtime" in resp.json()["detail"]

    async def test_start_unavailable_binary_400(self, api_app, api_client, auth_headers):
        """Runtime with unresolved binary returns 400."""
        tmux_svc = _make_mock_tmux_svc(session_exists_result=False)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        fake_runtimes = {
            "broken": {
                "display_name": "Broken",
                "command": "broken-bin",
                "resolved_path": None,
            },
        }
        try:
            with patch(f"{_AGENTS}._RUNTIMES", fake_runtimes):
                resp = await api_client.post(
                    "/api/agents/my-repo/start",
                    json={"runtime": "broken"},
                    headers=auth_headers,
                )
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 400
        assert "binary not found" in resp.json()["detail"]

    async def test_start_idempotent_already_existed(self, api_app, api_client, auth_headers):
        """Starting an already-existing session returns ok with already_existed=True."""
        tmux_svc = _make_mock_tmux_svc(session_exists_result=True)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        try:
            resp = await api_client.post("/api/agents/ike/start", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["already_existed"] is True
        tmux_svc.start_session.assert_not_awaited()

    async def test_start_unresolvable_dir_400(self, api_app, api_client, auth_headers):
        """Unresolvable working directory without explicit dir returns 400."""
        tmux_svc = _make_mock_tmux_svc(session_exists_result=False)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        try:
            with patch("agent_backbone.api.routes.agents.resolve_agent_dir", return_value=""):
                resp = await api_client.post("/api/agents/unknown-xyz/start", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 400
        assert "working_directory" in resp.json()["detail"]

    async def test_start_explicit_working_dir(self, api_app, api_client, auth_headers):
        """Explicit working_directory bypasses resolve_agent_dir."""
        tmux_svc = _make_mock_tmux_svc(session_exists_result=False)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        try:
            resp = await api_client.post(
                "/api/agents/custom/start",
                json={"working_directory": "/tmp/custom"},
                headers=auth_headers,
            )
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["working_directory"] == "/tmp/custom"
        call_kwargs = tmux_svc.start_session.call_args[1]
        assert call_kwargs["working_dir"] == "/tmp/custom"

    async def test_start_shell_no_command(self, api_app, api_client, auth_headers):
        """Shell runtime starts session with command=None."""
        tmux_svc = _make_mock_tmux_svc(session_exists_result=False)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        try:
            with patch(
                "agent_backbone.api.routes.agents.resolve_agent_dir",
                return_value="/tmp/shell-dir",
            ):
                resp = await api_client.post(
                    "/api/agents/my-shell/start",
                    json={"runtime": "shell"},
                    headers=auth_headers,
                )
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["runtime"] == "shell"
        call_kwargs = tmux_svc.start_session.call_args[1]
        assert call_kwargs["command"] is None
        assert call_kwargs["environment"] == {"BACKBONE_RUNTIME": "shell"}

    async def test_start_shell_ignores_model(self, api_app, api_client, auth_headers):
        """Shell runtime ignores model — response model is null."""
        tmux_svc = _make_mock_tmux_svc(session_exists_result=False)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        try:
            with patch(
                "agent_backbone.api.routes.agents.resolve_agent_dir",
                return_value="/tmp/shell-dir",
            ):
                resp = await api_client.post(
                    "/api/agents/my-shell/start",
                    json={"runtime": "shell", "model": "claude-opus-4-6"},
                    headers=auth_headers,
                )
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] is None


# ---------------------------------------------------------------------------
# POST /api/agents/{session}/stop
# ---------------------------------------------------------------------------


class TestStopAgent:
    async def test_stop_existing_session(self, api_app, api_client, auth_headers):
        """Stopping an existing session returns ok=True."""
        tmux_svc = _make_mock_tmux_svc(stop_session_result=True)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        try:
            resp = await api_client.post("/api/agents/ike/stop", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session"] == "ike"
        tmux_svc.stop_session.assert_awaited_once_with("ike")

    async def test_stop_preserves_session_case(self, api_app, api_client, auth_headers):
        """Mixed-case session names are preserved through stop."""
        tmux_svc = _make_mock_tmux_svc(stop_session_result=True)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        try:
            resp = await api_client.post("/api/agents/AI-chatbot/stop", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] == "AI-chatbot"
        tmux_svc.stop_session.assert_awaited_once_with("AI-chatbot")

    async def test_stop_nonexistent_idempotent(self, api_app, api_client, auth_headers):
        """Stopping a nonexistent session is idempotent (ok=True from tmux layer)."""
        tmux_svc = _make_mock_tmux_svc(stop_session_result=True)
        _set_di_overrides(api_app, tmux_svc=tmux_svc)
        try:
            resp = await api_client.post("/api/agents/ghost/stop", headers=auth_headers)
        finally:
            _clear_di_overrides(api_app)

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session"] == "ghost"


# ---------------------------------------------------------------------------
# POST /api/agents/{session}/state
# ---------------------------------------------------------------------------


class TestPostAgentState:
    async def test_post_state_stores_and_returns_ok(self, api_app, api_client, auth_headers):
        """POST state stores in DB and returns ok."""
        resp = await api_client.post(
            "/api/agents/feynman/state",
            json={
                "entity": "feynman",
                "state": "processing_issue",
                "issue": 571,
                "context": "Phase 1",
                "ts": 1709500000.0,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session"] == "feynman"

    async def test_post_state_readable_from_db(self, api_app, api_client, auth_headers):
        """State POSTed to the endpoint is readable from DB."""
        await api_client.post(
            "/api/agents/ike/state",
            json={"state": "idle", "entity": "ike", "ts": 100.0},
            headers=auth_headers,
        )
        db = api_app.state.db
        row = await db.get_agent_state("ike")
        assert row is not None
        assert row["state"] == "idle"
        assert row["entity"] == "ike"
        assert row["ts"] == "100.0"

    async def test_post_state_preserves_case(self, api_app, api_client, auth_headers):
        """Mixed-case session names are preserved."""
        resp = await api_client.post(
            "/api/agents/AI-chatbot/state",
            json={"state": "idle"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["session"] == "AI-chatbot"

    async def test_post_state_with_plan(self, api_app, api_client, auth_headers):
        """Plan file and title are stored."""
        await api_client.post(
            "/api/agents/ike/state",
            json={
                "state": "plan_waiting",
                "plan_file": "/tmp/plan.md",
                "plan_title": "Add caching",
                "ts": 200.0,
            },
            headers=auth_headers,
        )
        db = api_app.state.db
        row = await db.get_agent_state("ike")
        assert row["plan_file"] == "/tmp/plan.md"
        assert row["plan_title"] == "Add caching"

    async def test_post_state_invalidates_cache(self, api_app, api_client, auth_headers):
        """POST state resets the agents list cache TTL."""
        import agent_backbone.api.routes.agents as agents_mod

        agents_mod._agents_cache_ts = time.monotonic()
        await api_client.post(
            "/api/agents/ike/state",
            json={"state": "idle"},
            headers=auth_headers,
        )
        assert agents_mod._agents_cache_ts == 0


# ---------------------------------------------------------------------------
# POST /api/agents/{session}/activity
# ---------------------------------------------------------------------------


class TestPostAgentActivity:
    async def test_post_activity_returns_id(self, api_app, api_client, auth_headers):
        """POST activity returns ok with row ID."""
        resp = await api_client.post(
            "/api/agents/feynman/activity",
            json={"event": "tool_use", "ts": 1709500001.0, "tool": "Edit", "target": "config.py"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["id"] > 0

    async def test_post_activity_extra_fields_in_data(self, api_app, api_client, auth_headers):
        """Extra fields beyond event/ts are stored in the data JSON."""
        await api_client.post(
            "/api/agents/ike/activity",
            json={"event": "tool_use", "ts": 100.0, "tool": "Read", "file": "main.py"},
            headers=auth_headers,
        )
        db = api_app.state.db
        rows = await db.get_activity("ike")
        assert len(rows) == 1
        import json

        data = json.loads(rows[0]["data"])
        assert data["tool"] == "Read"
        assert data["file"] == "main.py"

    async def test_post_activity_preserves_case(self, api_app, api_client, auth_headers):
        """Mixed-case session names are preserved."""
        resp = await api_client.post(
            "/api/agents/AI-chatbot/activity",
            json={"event": "session_start", "ts": 100.0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        db = api_app.state.db
        rows = await db.get_activity("AI-chatbot")
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# GET /api/agents/{session}/activity
# ---------------------------------------------------------------------------


class TestGetAgentActivity:
    async def test_get_activity_returns_events(self, api_app, api_client, auth_headers):
        """GET activity returns recorded events."""
        db = api_app.state.db
        await db.record_activity("ike", "session_start", None, "1709500000.0")
        await db.record_activity("ike", "tool_use", '{"tool":"Edit"}', "1709500001.0")

        resp = await api_client.get("/api/agents/ike/activity", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        # Newest first
        assert data["items"][0]["event"] == "tool_use"
        assert data["items"][0]["data"] == {"tool": "Edit"}
        assert data["items"][1]["event"] == "session_start"
        assert data["items"][1]["data"] is None

    async def test_get_activity_respects_limit(self, api_app, api_client, auth_headers):
        """GET activity respects limit parameter."""
        db = api_app.state.db
        for i in range(10):
            await db.record_activity("ike", f"event_{i}", None, str(100 + i))

        resp = await api_client.get("/api/agents/ike/activity?limit=3", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3

    async def test_get_activity_since_filter(self, api_app, api_client, auth_headers):
        """GET activity filters by since parameter."""
        db = api_app.state.db
        await db.record_activity("ike", "old", None, "100.0")
        await db.record_activity("ike", "new", None, "200.0")

        resp = await api_client.get("/api/agents/ike/activity?since=150.0", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["event"] == "new"

    async def test_get_activity_empty_session(self, api_app, api_client, auth_headers):
        """GET activity for unknown session returns empty list."""
        resp = await api_client.get("/api/agents/nobody/activity", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []
